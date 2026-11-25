"""RuntimeIamStack: the data-plane policy for the agent runtime's execution role.

Imports the role by name (``-c exec_role_name``) and attaches one inline policy: the session store,
the Memory data plane, the run ledger, the sandbox session TTL and scheduled jobs, and the sandbox
runtime. The sandbox grant names only the sandbox runtime's ARN, so the agent cannot invoke or stop
itself or any other runtime. Synthesized only when ``-c exec_role_name`` is given, after
AgentRuntimeStack.
"""

from __future__ import annotations

from collections.abc import Sequence

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from .common import (
    MEMORY_RUNTIME_ACTIONS,
    Naming,
    memory_arn,
    sandbox_ttl_statements,
    schedules_statements,
    session_store_statements,
)


class RuntimeIamStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        exec_role_names: Sequence[str],
        memory_id: str | None = None,
        sandbox_runtime_arn: str | None = None,
        run_ledger_table: str | None = None,
        work_product_table: str | None = None,
        config_secret_arn: str | None = None,
        session_bucket: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        statements = [
            # The one AgentCore Memory: the transcript (CreateEvent per message) and long-term memory
            # (RetrieveMemoryRecords for recall; records are written only by the Memory's
            # extraction). Nothing broader, on nothing else.
            iam.PolicyStatement(
                sid="MemoryDataPlane",
                actions=list(MEMORY_RUNTIME_ACTIONS),
                resources=[memory_arn(self.region, self.account, naming, memory_id)],
            ),
            # GPT-6 Astra on Bedrock Mantle (the OpenAI-compatible Responses-API engine): the model
            # provider mints a SigV4 bearer token and calls the Mantle endpoint directly, which
            # authorizes against these bedrock-mantle actions — NOT bedrock:InvokeModel. Without
            # this the gpt-6-astra preset 401s on the first model call. Resource is "*"
            # because the service exposes no scopable resource-ARN format (matches the
            # BedrockMantle grant in backend_stack.py).
            iam.PolicyStatement(
                sid="BedrockMantleInference",
                actions=["bedrock-mantle:CreateInference", "bedrock-mantle:CallWithBearerToken"],
                resources=["*"],
            ),
        ]
        if sandbox_runtime_arn:
            # The agent drives its sandbox: one no-op invoke to provision the session, then shell
            # commands, and (rarely) an explicit stop. Scoped to the sandbox runtime's ARN — plus its
            # endpoint children, since a qualified invoke addresses `<runtime-arn>/runtime-endpoint/*`
            # — so this grant can never reach another runtime.
            statements.append(
                iam.PolicyStatement(
                    sid="SandboxRuntime",
                    actions=[
                        "bedrock-agentcore:InvokeAgentRuntime",
                        "bedrock-agentcore:InvokeAgentRuntimeCommand",
                        "bedrock-agentcore:StopRuntimeSession",
                    ],
                    resources=[sandbox_runtime_arn, f"{sandbox_runtime_arn}/*"],
                )
            )
            # The sandbox sessions' sliding TTL (harness sandbox/ttl.py): upsert one-time schedules
            # in the TTL group and pass the TTL scheduler role to Scheduler — never delete a session
            # directly (only the scheduler role can, when a schedule fires). Both names are
            # deterministic (Naming), so this needs no Sandbox stack import.
            statements.extend(sandbox_ttl_statements(naming, self.region, self.account))
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
        if work_product_table:
            # The runtime persists a WorkProduct row per review/triage run (agentcore_app →
            # WorkProductStore.put). Without this grant the write raises AccessDenied and, because
            # the store is fail-open, the run still succeeds while the portal's Today/Review/Triage
            # views stay permanently empty — a silent prod failure. PutItem is enough: the runtime
            # only ever writes fresh draft rows; curation edits go through the dashboard Lambda.
            statements.append(
                iam.PolicyStatement(
                    sid="WorkProductLedger",
                    actions=["dynamodb:PutItem"],
                    resources=[
                        f"arn:aws:dynamodb:{self.region}:{self.account}:table/{work_product_table}"
                    ],
                )
            )
        if session_bucket:
            statements.extend(session_store_statements(
                    session_bucket, naming, self.region, self.account, write=True
                ))
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

        # The harness's custom metrics (Invocations, Completed, ToolErrors, SessionWriteFailed, …),
        # put DIRECTLY with cloudwatch:PutMetricData: EMF extraction from a runtime's stdout is not
        # a platform contract (strandly.ops.metrics, docs/operations.md).
        # PutMetricData has no resource ARNs; the namespace condition pins the grant to this env's
        # Strandly namespace (the L2's own grant covers only ``bedrock-agentcore``).
        # The agent's `schedule` tool (the Scheduler stack's fixed names; see common.py).
        statements.extend(schedules_statements(naming, self.region, self.account))
        statements.append(
            iam.PolicyStatement(
                sid="StrandlyMetricsPut",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": naming.metrics_namespace}},
            )
        )

        # ONE policy on every role named (a deploy names exactly the AgentRuntime stack's role).
        # Sorted so the template does not depend on the order the roles were passed in.
        roles = sorted(set(exec_role_names))
        if not roles:
            raise ValueError("RuntimeIamStack needs at least one exec role name")
        iam.Policy(
            self,
            "DataPlanePolicy",
            policy_name=f"{naming.hyphen}-dataplane-access",
            statements=statements,
            roles=[
                iam.Role.from_role_name(self, "ExecRole" if i == 0 else f"ExecRole{i}", name)
                for i, name in enumerate(roles)
            ],
        )

        CfnOutput(self, "PolicyAttachedTo", value=",".join(roles))
