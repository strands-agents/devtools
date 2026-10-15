"""RuntimeIamStack — the supplemental data-plane policy for the runtime's execution role.

The bedrock-agentcore starter toolkit (``strandly deploy``) auto-creates the runtime's execution
role, but that role only grants the AWS *system* code interpreter (``aws.codeinterpreter.v1``). It
lacks our **custom** Code Interpreter, the **Memory** data plane, the **KB**, and (optionally) the
**run-ledger** table. This stack attaches exactly those grants — replacing the manual
``aws iam put-role-policy`` step that used ``deploy/execution-role-dataplane-policy.json``.

Because the toolkit owns the role and only names it after deploy, this stack imports the role **by
name** (``-c exec_role_name=…``) and attaches a managed policy to it. So the flow is:
``cdk deploy`` (Backend/Data) → ``strandly deploy`` (toolkit creates the role) → ``cdk deploy`` this
stack with the resolved role name + KB id + run-ledger table name.

This stack is **opt-in**: ``app.py`` only instantiates it when ``-c exec_role_name`` is supplied.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from .common import Naming


class RuntimeIamStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        exec_role_name: str,
        kb_id: str | None = None,
        run_ledger_table: str | None = None,
        config_secret_arn: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        agentcore_resource = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
        statements = [
            iam.PolicyStatement(
                sid="CustomCodeInterpreter",
                actions=[
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:GetCodeInterpreter",
                    "bedrock-agentcore:GetCodeInterpreterSession",
                    "bedrock-agentcore:ListCodeInterpreterSessions",
                ],
                resources=[agentcore_resource],
            ),
            iam.PolicyStatement(
                sid="MemoryDataPlane",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[agentcore_resource],
            ),
        ]
        if kb_id:
            statements.append(
                iam.PolicyStatement(
                    sid="KBLongTermMemory",
                    actions=[
                        "bedrock:Retrieve",
                        "bedrock:IngestKnowledgeBaseDocuments",
                        "bedrock:StartIngestionJob",
                        "bedrock:GetIngestionJob",
                        "bedrock:GetKnowledgeBase",
                    ],
                    resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb_id}"],
                )
            )
        if run_ledger_table:
            statements.append(
                iam.PolicyStatement(
                    sid="RunLedger",
                    actions=["dynamodb:PutItem"],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/{run_ledger_table}"
                    ],
                )
            )
        if config_secret_arn:
            # Let the runtime read the config secret at startup (Config.load with STRANDLY_SECRETS_ARN)
            # so rotating the GitHub token / backend ids is a secret update — no runtime redeploy. The
            # toolkit-created exec role otherwise lacks Secrets Manager access. Granted on the exact
            # (suffixed) secret ARN the runtime is deployed to read.
            statements.append(
                iam.PolicyStatement(
                    sid="ConfigSecretRead",
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[config_secret_arn],
                )
            )

        role = iam.Role.from_role_name(self, "ExecRole", exec_role_name)
        iam.Policy(
            self,
            "DataPlanePolicy",
            policy_name=f"{naming.hyphen}-dataplane-access",
            statements=statements,
            roles=[role],
        )

        CfnOutput(self, "PolicyAttachedTo", value=exec_role_name)
