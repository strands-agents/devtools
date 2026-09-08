"""BackendStack — the AgentCore + KB backends a deployed runtime uses, plus the config secret.

This replaces the imperative ``strandly provision`` (``src/strandly_harness/provisioning/``). Every
resource that module created with boto3 lookup-then-create + readiness polling is here as a
CloudFormation resource, so ordering, idempotency, and "wait until ACTIVE" are handled by the deploy
engine — the 10s IAM-propagation sleep, the ``_create_with_iam_retry`` loop, and the ``_poll`` loops
all simply disappear.

Creates:
- **AgentCore Memory** (short-term conversation session) — ``AWS::BedrockAgentCore::Memory``.
- **AgentCore Code Interpreter** (managed sandbox) — ``AWS::BedrockAgentCore::CodeInterpreterCustom``.
- **S3 Vectors bucket + index** (long-term memory vector store).
- **KB IAM role** — assumed by Bedrock; invoke embeddings + access the vector index.
- **Bedrock Knowledge Base** (S3-Vectors backed) + a **CUSTOM data source** (what ``add_memory``
  writes to).
- **Secrets Manager secret** holding the harness config (every id above + any tokens passed via
  ``-c github_token=…``), so a deployed runtime just sets ``STRANDLY_SECRETS_ARN``.

The secret payload mirrors what ``provisioning`` wrote, so nothing downstream (``config.py``, the
runtime) has to change. The KB can be skipped with ``-c with_kb=false``.
"""

from __future__ import annotations

import json

from aws_cdk import CfnOutput, CfnTag, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .common import (
    AGENT_TAG_VALUE,
    CODE_INTERPRETER_NETWORK_MODE,
    INFRA_TAG_VALUE,
    KB_EMBEDDING_MODEL,
    KB_VECTOR_DIMENSION,
    KB_VECTOR_DISTANCE_METRIC,
    MANAGED_BY_TAG_KEY,
    MANAGED_NAME_PREFIX,
    MEMORY_EVENT_EXPIRY_DAYS,
    Naming,
)


class BackendStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        with_kb: bool = True,
        extra_secrets: dict[str, str] | None = None,
        ci_bedrock_role: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        embedding_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{KB_EMBEDDING_MODEL}"

        # Prod backends carry ManagedBy=strandly-infra — a DIFFERENT value from the agent's
        # ManagedBy=strandly — so the (optional) CI sandbox role's tag-scoped grants can never reach
        # them. This tag is the load-bearing half of the self-protection boundary; keep it on.
        memory = agentcore.CfnMemory(
            self,
            "Memory",
            name=naming.memory,
            event_expiry_duration=MEMORY_EVENT_EXPIRY_DAYS,
            tags={MANAGED_BY_TAG_KEY: INFRA_TAG_VALUE},
        )
        code_interpreter = agentcore.CfnCodeInterpreterCustom(
            self,
            "CodeInterpreter",
            name=naming.code_interpreter,
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode=CODE_INTERPRETER_NETWORK_MODE
            ),
            execution_role_arn=(
                self._ci_execution_role(naming).role_arn if ci_bedrock_role else None
            ),
            tags={MANAGED_BY_TAG_KEY: INFRA_TAG_VALUE},
        )

        # The config secret payload — the ids the runtime reads (mirrors provisioning's secret).
        payload: dict[str, str] = {
            "AGENTCORE_MEMORY_ID": memory.attr_memory_id,
            "AGENTCORE_CODE_INTERPRETER_ID": code_interpreter.attr_code_interpreter_id,
        }

        if with_kb:
            kb_id, ds_id = self._knowledge_base(naming, embedding_arn)
            payload["STRANDLY_KB_ID"] = kb_id
            payload["STRANDLY_KB_DATA_SOURCE_ID"] = ds_id

        for key, value in (extra_secrets or {}).items():
            payload[key] = value

        # The secret string carries CloudFormation tokens (the attr_* ids). ``json.dumps`` over a
        # token yields a ``"${Token[...]}"`` placeholder inside the string that CDK swaps for the
        # real ``Fn::GetAtt`` / ``Ref`` at synth — so the deployed secret holds the resolved ids.
        # We use CfnSecret directly (not the L2 Secret, which random-generates a SecretString).
        secret = secretsmanager.CfnSecret(
            self,
            "ConfigSecret",
            name=naming.secret,
            secret_string=json.dumps(payload),
        )
        secret.apply_removal_policy(
            RemovalPolicy.RETAIN if naming.env == "prod" else RemovalPolicy.DESTROY
        )

        CfnOutput(self, "SecretArn", value=secret.ref)
        CfnOutput(self, "SecretName", value=naming.secret)
        CfnOutput(self, "MemoryId", value=memory.attr_memory_id)
        CfnOutput(self, "CodeInterpreterId", value=code_interpreter.attr_code_interpreter_id)

    def _ci_execution_role(self, naming: Naming) -> iam.Role:
        """The Code Interpreter's execution role for e2e-testing Strands against live Bedrock.

        Opt-in (``-c ci_bedrock_role=true``). Credentials surface inside the sandbox via MMDS, so
        **everything granted here is reachable by — and exfiltratable from — code the agent runs.**
        Two boundaries keep that safe:

        - **Invoke-only on models** (no Bedrock control plane for inference): scoped to
          ``foundation-model/*`` + ``inference-profile/*`` so the agent can run any Strands test
          model without a role edit, but can't manage models.
        - **ABAC tag boundary on lifecycle**: it may *create* KB / guardrail / data-source / bucket
          resources only when it tags them ``ManagedBy=strandly`` (``aws:RequestTag``), and may
          read/update/delete only resources already tagged so (``aws:ResourceTag``). Prod backends
          are tagged ``strandly-infra``, so they're invisible to these grants — the agent cannot
          touch its own production KB/Memory/runtime.

        Deliberately withheld: ``iam:CreateRole``/``CreatePolicy`` (role-creation from a sandbox is a
        privilege-escalation hole). A KB needs a service role, so we pre-create ONE fixed role and
        grant ``iam:PassRole`` on only that ARN — the agent reuses it for ``ManagedBy=strandly`` KBs
        without minting new roles.
        """
        agent_tag = {f"aws:RequestTag/{MANAGED_BY_TAG_KEY}": AGENT_TAG_VALUE}
        resource_tag = {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": AGENT_TAG_VALUE}

        role = iam.Role(
            self,
            "CiExecutionRole",
            role_name=f"{naming.under}_ci_exec",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Strandly Code Interpreter sandbox: invoke Bedrock + manage ManagedBy=strandly test resources.",
        )

        # 0. CLOSE THE RE-TAG ESCALATION. Without this, the ABAC boundary is bypassable: the agent
        #    could ListKnowledgeBases → find a prod KB (ManagedBy=strandly-infra) → TagResource it to
        #    ManagedBy=strandly (satisfies the RequestTag gate on statement 2, and tagging overwrites
        #    by key) → then Delete/Retrieve it via statement 3's ResourceTag gate. An explicit Deny
        #    always beats an Allow, so denying (Un)TagResource on anything already tagged
        #    strandly-infra makes prod un-re-taggable while still allowing tagging of freshly-created
        #    untagged resources. This is the load-bearing guard for the whole self-protection claim.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyRetagInfra",
                effect=iam.Effect.DENY,
                actions=["bedrock:TagResource", "bedrock:UntagResource"],
                resources=["*"],
                conditions={"StringEquals": {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": INFRA_TAG_VALUE}},
            )
        )

        # 1. Model invocation (+ Mantle) — invoke-only, any model, no control plane.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "bedrock:CountTokens",
                    "bedrock:ApplyGuardrail",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockMantle",
                actions=["bedrock-mantle:CreateInference", "bedrock-mantle:CallWithBearerToken"],
                resources=["*"],  # no Mantle resource ARN format exists to scope to
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(sid="StsWhoami", actions=["sts:GetCallerIdentity"], resources=["*"])
        )

        # 2. Create lifecycle — only if the agent tags the new resource ManagedBy=strandly.
        #    Create* has no pre-existing ARN, so Resource is * and the RequestTag condition is the
        #    gate (the standard ABAC create pattern). aws:TagKeys is pinned so the request can carry
        #    ONLY the ManagedBy key — no smuggling extra tags alongside it. (Statement 0's Deny stops
        #    this from re-tagging an existing strandly-infra resource.)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CreateManagedResources",
                actions=[
                    "bedrock:CreateKnowledgeBase",
                    "bedrock:CreateDataSource",
                    "bedrock:CreateGuardrail",
                    "bedrock:TagResource",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": agent_tag,
                    "ForAllValues:StringEquals": {"aws:TagKeys": [MANAGED_BY_TAG_KEY]},
                },
            )
        )
        # 3. Operate lifecycle — only on resources already tagged ManagedBy=strandly.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="OperateManagedResources",
                actions=[
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:IngestKnowledgeBaseDocuments",
                    "bedrock:GetKnowledgeBaseDocuments",
                    "bedrock:ListKnowledgeBaseDocuments",
                    "bedrock:DeleteKnowledgeBaseDocuments",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:GetKnowledgeBase",
                    "bedrock:UpdateKnowledgeBase",
                    "bedrock:DeleteKnowledgeBase",
                    "bedrock:GetDataSource",
                    "bedrock:DeleteDataSource",
                    "bedrock:GetGuardrail",
                    "bedrock:UpdateGuardrail",
                    "bedrock:DeleteGuardrail",
                    "bedrock:UntagResource",
                ],
                resources=["*"],
                conditions={"StringEquals": resource_tag},
            )
        )
        # List* can't be ABAC-scoped (no resource at list time); read-only, low risk.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListAndDiscover",
                actions=[
                    "bedrock:ListKnowledgeBases",
                    "bedrock:ListDataSources",
                    "bedrock:ListGuardrails",
                    "bedrock:ListFoundationModels",
                    "bedrock:GetFoundationModel",
                ],
                resources=["*"],
            )
        )

        # 4. A fixed KB service role the agent PASSES to CreateKnowledgeBase (never creates its own).
        kb_test_role = iam.Role(
            self,
            "ManagedKbRole",
            role_name=f"{naming.under}_managed_kb_role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Service role the sandbox passes to CreateKnowledgeBase for ManagedBy=strandly test KBs.",
        )
        kb_test_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["arn:aws:bedrock:*::foundation-model/*"],
            )
        )
        kb_test_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    f"arn:aws:s3:::{MANAGED_NAME_PREFIX}-*",
                    f"arn:aws:s3:::{MANAGED_NAME_PREFIX}-*/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassKbServiceRole",
                actions=["iam:PassRole"],
                resources=[kb_test_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock.amazonaws.com"}},
            )
        )

        # 5. S3 for test media + KB sources — name-scoped to strandly-managed-* AND tag-gated on
        #    bucket create (defence in depth: both the name prefix and the tag must hold).
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ManagedBuckets",
                actions=[
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
                resources=[
                    f"arn:aws:s3:::{MANAGED_NAME_PREFIX}-*",
                    f"arn:aws:s3:::{MANAGED_NAME_PREFIX}-*/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3CreateManagedBuckets",
                actions=["s3:CreateBucket"],
                resources=[f"arn:aws:s3:::{MANAGED_NAME_PREFIX}-*"],
            )
        )

        CfnOutput(self, "CiExecutionRoleArn", value=role.role_arn)
        return role

    def _knowledge_base(self, naming: Naming, embedding_arn: str) -> tuple[str, str]:
        """S3-Vectors bucket+index, KB role, KB, and the CUSTOM data source. Returns (kb_id, ds_id)."""
        bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=naming.vector_bucket,
            tags=[CfnTag(key=MANAGED_BY_TAG_KEY, value=INFRA_TAG_VALUE)],
        )
        index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=naming.vector_index,
            vector_bucket_name=naming.vector_bucket,
            data_type="float32",
            dimension=KB_VECTOR_DIMENSION,
            distance_metric=KB_VECTOR_DISTANCE_METRIC,
        )
        index.add_dependency(bucket)

        # KB role: trust bedrock.amazonaws.com (scoped to this account), invoke embeddings, full
        # access to the vector store. Mirrors provisioning/_ensure_kb_role.
        kb_role = iam.Role(
            self,
            "KbRole",
            role_name=naming.kb_role,
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Strandly long-term-memory KB: invoke embeddings + access the S3 vector index.",
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[embedding_arn])
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:*"],
                resources=[
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/*",
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/*/index/*",
                ],
            )
        )

        kb = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=naming.kb,
            role_arn=kb_role.role_arn,
            tags={MANAGED_BY_TAG_KEY: INFRA_TAG_VALUE},
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_arn
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=bucket.attr_vector_bucket_arn,
                    index_arn=index.attr_index_arn,
                ),
            ),
        )
        kb.add_dependency(index)
        kb.node.add_dependency(kb_role)

        data_source = bedrock.CfnDataSource(
            self,
            "DataSource",
            knowledge_base_id=kb.attr_knowledge_base_id,
            name=naming.data_source,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="CUSTOM"
            ),
        )

        CfnOutput(self, "KnowledgeBaseId", value=kb.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=data_source.attr_data_source_id)
        return kb.attr_knowledge_base_id, data_source.attr_data_source_id
