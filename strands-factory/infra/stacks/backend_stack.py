"""BackendStack: AgentCore Memory, Code Interpreter and the harness config secret.

Every id a deployed runtime needs lands in one Secrets Manager secret, so the runtime sets only
``STRANDLY_SECRETS_ARN``. The retired knowledge base (S3 Vectors bucket and index, role, knowledge
base and data source) stays in the template with ``RETAIN`` until the one-off migration is verified
(``-c with_kb``, default off; existing deployments set it on).
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, CfnTag, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from constructs import Construct

from .common import (
    CODE_INTERPRETER_NETWORK_MODE,
    KB_EMBEDDING_MODEL,
    KB_VECTOR_DIMENSION,
    KB_VECTOR_DISTANCE_METRIC,
    MANAGED_BY_TAG_KEY,
    MEMORY_EVENT_EXPIRY_DAYS,
    MEMORY_FACTS_NAMESPACE_TEMPLATE,
    MEMORY_FACTS_STRATEGY,
    MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
    MEMORY_PREFERENCES_STRATEGY,
    Naming,
)
from .runtime_common import bounded_log_group

# infra/stacks/backend_stack.py -> infra/ -> infra/lambdas/merge_secret_payload/
_MERGE_SECRET_PAYLOAD_LAMBDA_DIR = (
    Path(__file__).resolve().parents[1] / "lambdas" / "merge_secret_payload"
)


def memory_strategies() -> list[agentcore.CfnMemory.MemoryStrategyProperty]:
    """The Memory's long-term strategies: semantic facts and user preferences, per actor.

    The transcript writer sends conversation text as ``conversational`` payload items (extraction
    on; a reply's reasoning rides along as a ``blob`` item, which is not extracted) and everything
    else with ``extractionMode=SKIP``, so these strategies only ever see conversation text.
    ``{actorId}`` is ``team`` for team runs and ``slack-user-<UID>`` for a Slack DM
    (``strandly.memory.scopes``), so a DM is extracted into that person's namespaces only.
    No summary or episodic strategy: recall reads facts and preferences (the team's on every run,
    plus the person's in a DM); per-session summaries would add records nothing reads.

    ``MemoryStrategies`` and ``EventExpiryDuration`` update in place (only ``Name`` and
    ``EncryptionKeyArn`` are create-only), so adding them keeps the resource and its events.
    """
    return [
        agentcore.CfnMemory.MemoryStrategyProperty(
            semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                name=MEMORY_FACTS_STRATEGY,
                description="Facts from team and personal conversations.",
                namespaces=[MEMORY_FACTS_NAMESPACE_TEMPLATE],
            )
        ),
        agentcore.CfnMemory.MemoryStrategyProperty(
            user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                name=MEMORY_PREFERENCES_STRATEGY,
                description="Working preferences of the person in a conversation.",
                namespaces=[MEMORY_PREFERENCES_NAMESPACE_TEMPLATE],
            )
        ),
    ]


class BackendStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        with_kb: bool = False,
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
            memory_strategies=memory_strategies(),
            tags={MANAGED_BY_TAG_KEY: naming.infra_tag},
        )
        # The Memory now holds every long-term memory (team and personal) and the transcripts, so a
        # stack deletion or an accidental replacement must never take it with it in prod. Changing
        # the policy is metadata only: no update, no replacement.
        memory.apply_removal_policy(
            RemovalPolicy.RETAIN if naming.env == "prod" else RemovalPolicy.DESTROY
        )
        active_code_interpreter = agentcore.CfnCodeInterpreterCustom(
            self,
            "CodeInterpreterV2",
            name=naming.code_interpreter_v2,
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode=CODE_INTERPRETER_NETWORK_MODE
            ),
            execution_role_arn=(
                self._ci_execution_role(naming).role_arn if ci_bedrock_role else None
            ),
            tags={MANAGED_BY_TAG_KEY: naming.infra_tag},
        )

        # The config secret payload — the CDK-DERIVED ids the runtime reads (mirrors provisioning's
        # secret). STRANDLY_RUN_LEDGER_TABLE is derived by NAME (same no-cross-stack-import
        # rationale as DashboardStack/IngressStack referencing Data's tables) rather than importing
        # DataStack's live ITable — Backend and Data are independent stacks with no ordering
        # dependency here.
        payload: dict[str, str] = {
            # The Memory holds the transcript (one event per message) and long-term memory
            # (records under /facts/... and /preferences/...).
            "AGENTCORE_MEMORY_ID": memory.attr_memory_id,
            "AGENTCORE_CODE_INTERPRETER_ID": active_code_interpreter.attr_code_interpreter_id,
            "STRANDLY_RUN_LEDGER_TABLE": naming.run_ledger_table,
            # Same by-name derivation as the run-ledger: without this the deployed runtime's
            # Config.work_product_table is None, so WorkProductStore is disabled (fail-open) and the
            # agent silently never persists review/triage drafts — the portal would show no data.
            "STRANDLY_WORK_PRODUCT_TABLE": naming.work_product_table,
            # The session store (DataStack's sessions bucket), same by-name derivation. This is what
            # switches the deployed runtime's short-term session from local files to S3 — without
            # it every cold start would begin an empty conversation.
            "STRANDLY_SESSION_BUCKET": naming.session_bucket(self.account),
            # The metric namespace the deployed runtime emits its EMF under. Every *Lambda* surface
            # gets this wired directly onto its function env (ingress/slack/monitoring stacks), but
            # the AgentCore **runtime** carries only its fixed deploy wiring (AgentRuntimeStack's six
            # variables), so the config secret is the channel every other value takes. Without
            # it `metrics.enabled()` was False in-runtime and every run-level emit (Invocations /
            # Completed / Failures / DurationMs / ToolErrors) was a silent no-op, so any alarm on
            # runtime success rate had no data behind it. Static per-env string (`Strandly-prod`),
            # so it never churns the custom resource on its own.
            "STRANDLY_METRICS_NAMESPACE": naming.metrics_namespace,
        }

        if with_kb:
            # Retired: kept only so its data survives until the migration is verified. Its ids are
            # not written to the config secret (nothing reads them).
            self._retired_knowledge_base(naming, embedding_arn)

        # generate_secret_string (not secret_string): CloudFormation only ever applies
        # GenerateSecretString on CREATE — an UPDATE to this stack does NOT touch the live secret
        # value at all. The MergeSecretPayload custom resource below is what actually keeps the
        # CDK-derived keys current, via a real read-merge-write against Secrets Manager — so any
        # key a human adds directly (STRANDLY_GITHUB_TOKEN, STRANDLY_MENTION_ALLOWED_AUTHORS, …)
        # is NEVER touched by a stack deploy, no matter how many times Backend redeploys.
        secret = secretsmanager.CfnSecret(
            self,
            "ConfigSecret",
            name=naming.secret,
            generate_secret_string=secretsmanager.CfnSecret.GenerateSecretStringProperty(
                secret_string_template="{}", generate_string_key="_unused"
            ),
        )
        secret.apply_removal_policy(
            RemovalPolicy.RETAIN if naming.env == "prod" else RemovalPolicy.DESTROY
        )
        self._merge_secret_payload(secret.ref, payload)

        CfnOutput(self, "SecretArn", value=secret.ref)
        CfnOutput(self, "SecretName", value=naming.secret)
        CfnOutput(self, "MemoryId", value=memory.attr_memory_id)
        CfnOutput(self, "CodeInterpreterId", value=active_code_interpreter.attr_code_interpreter_id)

    def _merge_secret_payload(self, secret_arn: str, payload: dict[str, str]) -> None:
        """Merge ``payload`` into the live secret — CREATE/UPDATE/DELETE all merge, never replace.

        Handler code: ``infra/lambdas/merge_secret_payload/handler.py`` (unit-tested directly,
        no CloudFormation event shape involved for the merge logic itself).

        Runs on every deploy (the resource's properties change whenever any CDK-derived id does,
        so CloudFormation always re-invokes it). GetSecretValue + PutSecretValue with the CDK keys
        overlaid on top of whatever is already there, so a human-added key (STRANDLY_GITHUB_TOKEN,
        STRANDLY_MENTION_ALLOWED_AUTHORS, …) is never at risk from a Backend stack deploy — the
        previous CfnSecret(secret_string=...) approach did a full replace and wiped exactly those
        keys in prod. DELETE is a deliberate no-op: removing this custom resource (e.g. a stack
        rollback) must not touch the secret at all.
        """
        handler = lambda_.Function(
            self,
            "MergeSecretPayloadFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            timeout=Duration.seconds(30),
            # Bytecode excluded: a local test run's __pycache__ would otherwise change the asset hash.
            code=lambda_.Code.from_asset(
                str(_MERGE_SECRET_PAYLOAD_LAMBDA_DIR), exclude=["**/__pycache__", "**/*.pyc"]
            ),
            log_group=bounded_log_group(self, "MergeSecretPayloadLogGroup"),
        )
        handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
                resources=[secret_arn],
            )
        )
        provider = cr.Provider(
            self,
            "MergeSecretPayloadProvider",
            on_event_handler=handler,
            log_group=bounded_log_group(self, "MergeSecretPayloadProviderLogGroup"),
        )
        CustomResource(
            self,
            "MergeSecretPayload",
            service_token=provider.service_token,
            properties={"SecretArn": secret_arn, "Payload": json.dumps(payload)},
        )

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
        agent_tag = {f"aws:RequestTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}
        resource_tag = {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}

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
                conditions={"StringEquals": {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.infra_tag}},
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
                    f"arn:aws:s3:::{naming.managed_prefix}-*",
                    f"arn:aws:s3:::{naming.managed_prefix}-*/*",
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
                    f"arn:aws:s3:::{naming.managed_prefix}-*",
                    f"arn:aws:s3:::{naming.managed_prefix}-*/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="S3CreateManagedBuckets",
                actions=["s3:CreateBucket"],
                resources=[f"arn:aws:s3:::{naming.managed_prefix}-*"],
            )
        )

        CfnOutput(self, "CiExecutionRoleArn", value=role.role_arn)
        return role

    def _retired_knowledge_base(self, naming: Naming, embedding_arn: str) -> None:
        """The retired long-term-memory KB (S3-Vectors bucket+index, KB role, KB, CUSTOM data source).

        Unchanged resources (same logical ids and properties, so a deploy changes nothing in
        them), every one ``RemovalPolicy.RETAIN``: removing them from the template later, or
        deleting the stack, leaves the data in place.
        """
        bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=naming.vector_bucket,
            tags=[CfnTag(key=MANAGED_BY_TAG_KEY, value=naming.infra_tag)],
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
        index.add_resource_dependency(bucket)

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
            tags={MANAGED_BY_TAG_KEY: naming.infra_tag},
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
        kb.add_resource_dependency(index)
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

        for resource in (bucket, index, kb, data_source):
            resource.apply_removal_policy(RemovalPolicy.RETAIN)
        kb_role.apply_removal_policy(RemovalPolicy.RETAIN)
        policy = kb_role.node.try_find_child("DefaultPolicy")
        if isinstance(policy, iam.Policy):  # keep the KB able to read its index after phase 2
            policy.apply_removal_policy(RemovalPolicy.RETAIN)
        # The migration reads the index by these names (deterministic, also in Naming).
        CfnOutput(self, "KnowledgeBaseId", value=kb.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=data_source.attr_data_source_id)
        CfnOutput(self, "RetiredVectorBucketName", value=naming.vector_bucket)
        CfnOutput(self, "RetiredVectorIndexName", value=naming.vector_index)
