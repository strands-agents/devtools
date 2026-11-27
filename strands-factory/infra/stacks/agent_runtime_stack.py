"""AgentRuntimeStack: the agent's AgentCore Runtime on the microVM, with its execution role.

The runtime ``<name>_<env>_agent`` ships the code bundle built by
``infra/scripts/build_agent_bundle.py`` (entrypoint ``opentelemetry-instrument main.py``) on
``PUBLIC`` networking, with a fixed-name execution role that holds only the agent's baseline grants;
data-plane grants are added by RuntimeIamStack. Its ``AgentRuntimeArn`` output feeds every trigger.
Each runtime's AgentCore TRACES go to X-Ray through a CloudWatch Logs delivery.
"""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from .agent_instances_standby import add_instances_standby, standby_runtime_name
from .common import (
    AGENT_IDLE_TIMEOUT_SECONDS,
    AGENT_MAX_LIFETIME_SECONDS,
    AGENT_PLATFORM_VERSION,
    Naming,
    schedules_environment,
)

#: ADOT wraps ``main.py`` (GenAI traces/logs on).
AGENT_ENTRYPOINT = ("opentelemetry-instrument", "main.py")

#: Set unconditionally: it states where the code runs, which is what makes a deployed runtime refuse
#: to execute shell commands without a sandbox
#: (strandly.core.constants.AGENTCORE_RUNTIME_MARKER_ENV).
AGENTCORE_RUNTIME_MARKER_ENV = "BEDROCK_AGENTCORE_RUNTIME"

#: The agent's OpenTelemetry sampler (``OTEL_TRACES_SAMPLER``). Not parent-based: every ingress
#: Lambda forwards its own unsampled X-Ray header on ``InvokeAgentRuntime``, which would turn
#: ADOT's default ``parentbased_always_on`` into "sample nothing".
DEFAULT_TRACES_SAMPLER = "always_on"
TRACES_SAMPLERS = frozenset(
    {
        "always_on",
        "always_off",
        "traceidratio",
        "parentbased_always_on",
        "parentbased_always_off",
        "parentbased_traceidratio",
    }
)

AGENT_DESCRIPTION = (
    "Strandly agent on the AgentCore microVM: model, config secret, Memory, run ledger. Shell/file "
    "work runs in the separate sandbox runtime."
)


def agent_environment(
    *,
    region: str,
    secret_arn: str,
    memory_id: str,
    sandbox_runtime_arn: str,
    sandbox_capacity_provider_id: str,
    sandbox_ttl_schedule_group: str,
    sandbox_ttl_role_arn: str,
    schedules: Mapping[str, str] | None = None,
    actions_schedule_repositories: str = "",
    traces_sampler: str = DEFAULT_TRACES_SAMPLER,
    traces_sampler_arg: str = "",
) -> dict[str, str]:
    """The agent runtime's environment (the three ``STRANDLY_SANDBOX_*TTL*`` ids drive the sandbox
    sessions' sliding delete schedule, ``sandbox/ttl.py``; ``schedules`` are the
    ``STRANDLY_SCHEDULES_*`` of the ``schedule`` tool; ``traces_sampler`` and
    ``traces_sampler_arg`` are ``OTEL_TRACES_SAMPLER`` and ``OTEL_TRACES_SAMPLER_ARG``).

    Raises:
        ValueError: If ``traces_sampler`` is not an OpenTelemetry SDK sampler name, or a ratio
            sampler has no ``traces_sampler_arg``.
    """
    if traces_sampler not in TRACES_SAMPLERS:
        raise ValueError(
            f"traces_sampler must be one of {', '.join(sorted(TRACES_SAMPLERS))}, "
            f"not {traces_sampler!r}"
        )
    if traces_sampler.endswith("traceidratio") and not traces_sampler_arg:
        raise ValueError(f"traces_sampler={traces_sampler} needs traces_sampler_arg (0..1)")
    env = {
        "AWS_REGION": region,
        "STRANDLY_SECRETS_ARN": secret_arn,
        "AGENTCORE_MEMORY_ID": memory_id,
        "STRANDLY_SANDBOX_RUNTIME_ARN": sandbox_runtime_arn,
        "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": sandbox_capacity_provider_id,
        "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": sandbox_ttl_schedule_group,
        "STRANDLY_SANDBOX_TTL_ROLE_ARN": sandbox_ttl_role_arn,
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_TRACES_SAMPLER": traces_sampler,
        **(schedules or {}),
    }
    if traces_sampler_arg:
        env["OTEL_TRACES_SAMPLER_ARG"] = traces_sampler_arg
    # Opt-in only: unset means no GitHub Actions run may change scheduled jobs.
    if actions_schedule_repositories:
        env["STRANDLY_ACTIONS_SCHEDULE_REPOSITORIES"] = actions_schedule_repositories
    env[AGENTCORE_RUNTIME_MARKER_ENV] = "true"  # last, never overridable
    return env


class AgentRuntimeStack(Stack):
    """The agent runtime (code-zip, microVM) and its exec role.

    Log retention for its service-created log group is set by MonitoringStack, like every runtime.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        code_asset_dir: str,
        secret_arn: str,
        memory_id: str,
        sandbox_runtime_arn: str,
        sandbox_capacity_provider_id: str,
        sandbox_ttl_schedule_group: str,
        sandbox_ttl_role_arn: str,
        agent_subnet_ids: str = "",
        agent_security_group_id: str = "",
        retain_instances_standby: bool = False,
        actions_schedule_repositories: str = "",
        schedules_max_active: int | None = None,
        traces_sampler: str = DEFAULT_TRACES_SAMPLER,
        traces_sampler_arg: str = "",
        **kwargs: object,
    ) -> None:
        """Create the agent runtime.

        Args:
            naming: Deployment naming.
            code_asset_dir: The built bundle (``infra/scripts/build_agent_bundle.py``).
            secret_arn / memory_id / sandbox_runtime_arn: The Backend and Sandbox stack outputs the
                runtime needs.
            sandbox_capacity_provider_id / sandbox_ttl_schedule_group / sandbox_ttl_role_arn: The
                Sandbox stack's TTL outputs (``CapacityProviderId``, ``SandboxTtlScheduleGroup``,
                ``SandboxTtlSchedulerRoleArn``), so the agent can slide each sandbox session's
                delete schedule.
            agent_subnet_ids / agent_security_group_id: The Sandbox stack's ``AgentSubnetIds`` /
                ``AgentSecurityGroupId`` outputs. Read only by the Instances standby; the microVM
                runtime ignores them.
            retain_instances_standby: Keep the retired Instances runtime and capacity provider in
                the stack for rollback.
            actions_schedule_repositories: Comma-separated repositories whose GitHub Actions runs
                may change scheduled jobs (``STRANDLY_ACTIONS_SCHEDULE_REPOSITORIES``).
            traces_sampler / traces_sampler_arg: The agent's ``OTEL_TRACES_SAMPLER`` and
                ``OTEL_TRACES_SAMPLER_ARG``.

        Raises:
            ValueError: If a required backend id is empty (a runtime without its secret, Memory or
                sandbox refuses to run — fail at synth, not at the first invocation), or the
                standby is kept without its network ids.
        """
        super().__init__(scope, construct_id, **kwargs)
        required = [
            ("secret_arn", secret_arn),
            ("memory_id", memory_id),
            ("sandbox_runtime_arn", sandbox_runtime_arn),
            ("sandbox_capacity_provider_id", sandbox_capacity_provider_id),
            ("sandbox_ttl_schedule_group", sandbox_ttl_schedule_group),
            ("sandbox_ttl_role_arn", sandbox_ttl_role_arn),
        ]
        subnets = [subnet.strip() for subnet in agent_subnet_ids.split(",") if subnet.strip()]
        if retain_instances_standby:
            required += [
                ("agent_subnet_ids", ",".join(subnets)),
                ("agent_security_group_id", agent_security_group_id),
            ]
        for label, value in required:
            if not value:
                raise ValueError(f"AgentRuntimeStack needs {label} (pass -c {label}=…)")

        name = naming.agent_runtime
        runtime_names = [name]
        if retain_instances_standby:
            runtime_names.append(standby_runtime_name(naming))

        role = iam.Role(
            self,
            "ExecRole",
            role_name=naming.agent_runtime_exec_role,
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            # A prefix: the role outlives a runtime replacement, during which the
                            # old and the new runtime (``<name>_<env>_agent…``) both hold it.
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/"
                            f"{naming.agent_runtime}*"
                        )
                    },
                },
            ),
            description=(
                "Execution role for the Strandly AGENT runtime (CDK-owned). Baseline here; Memory, "
                "ledger, config secret and sandbox grants come from the RuntimeIam stack."
            ),
        )
        self._add_baseline_grants(role, runtime_names)

        artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path=code_asset_dir,
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            entrypoint=list(AGENT_ENTRYPOINT),
            display_name=f"{name} code bundle",
        )
        environment = agent_environment(
            region=self.region,
            secret_arn=secret_arn,
            memory_id=memory_id,
            sandbox_runtime_arn=sandbox_runtime_arn,
            sandbox_capacity_provider_id=sandbox_capacity_provider_id,
            sandbox_ttl_schedule_group=sandbox_ttl_schedule_group,
            sandbox_ttl_role_arn=sandbox_ttl_role_arn,
            schedules=schedules_environment(
                naming, self.region, self.account, max_active=schedules_max_active
            ),
            actions_schedule_repositories=actions_schedule_repositories,
            traces_sampler=traces_sampler,
            traces_sampler_arg=traces_sampler_arg,
        )
        runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=name,
            agent_runtime_artifact=artifact,
            execution_role=role,
            description=AGENT_DESCRIPTION,
            platform_version=agentcore.PlatformVersion.of(AGENT_PLATFORM_VERSION),
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.seconds(AGENT_IDLE_TIMEOUT_SECONDS),
                max_lifetime=Duration.seconds(AGENT_MAX_LIFETIME_SECONDS),
            ),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables=environment,
        )

        self.role = role
        self.runtime = runtime
        self.traces_destination = logs.CfnDeliveryDestination(
            self,
            "TracesDestination",
            name=naming.agent_traces_destination,
            delivery_destination_type="XRAY",
        )
        self._add_traces_delivery("Traces", naming.agent_traces_source(name), runtime)
        CfnOutput(
            self,
            "AgentRuntimeArn",
            value=runtime.agent_runtime_arn,
            description="The agent runtime ARN — every trigger's -c runtime_arn.",
        )
        CfnOutput(self, "AgentRuntimeId", value=runtime.agent_runtime_id)
        CfnOutput(self, "AgentRuntimeName", value=name, description="The agent runtime's name.")
        CfnOutput(
            self,
            "AgentExecRoleName",
            value=role.role_name,
            description="The -c exec_role_name for the RuntimeIam and WebSearch stacks.",
        )

        if retain_instances_standby:
            standby, cp = add_instances_standby(
                self,
                naming=naming,
                role=role,
                artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                    path=code_asset_dir,
                    runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
                    entrypoint=list(AGENT_ENTRYPOINT),
                    display_name=f"{standby_runtime_name(naming)} code bundle",
                ),
                entrypoint_environment=environment,
                subnets=subnets,
                security_group_id=agent_security_group_id,
            )
            self.standby_runtime = standby
            self.capacity_provider = cp
            self._add_traces_delivery(
                "StandbyTraces", naming.agent_traces_source(standby_runtime_name(naming)), standby
            )
            CfnOutput(
                self,
                "StandbyAgentRuntimeArn",
                value=standby.agent_runtime_arn,
                description="The retired Instances runtime, kept for rollback. Nothing invokes it.",
            )
            CfnOutput(self, "AgentCapacityProviderArn", value=cp.attr_arn)

    def _add_traces_delivery(
        self, prefix: str, source_name: str, runtime: agentcore.Runtime
    ) -> logs.CfnDelivery:
        """Deliver ``runtime``'s AgentCore TRACES to X-Ray (the ``aws/spans`` log group).

        The source references the runtime ARN and the delivery references both ends, so
        CloudFormation creates them after the runtime and deletes them before it; a replaced
        runtime updates the source's ``ResourceArn`` in place. X-Ray accepts the spans through the
        account's resource policy for ``delivery.logs.amazonaws.com``, which AWS creates on the
        first delivery when the deploying principal holds ``xray:PutResourcePolicy``.
        """
        source = logs.CfnDeliverySource(
            self,
            f"{prefix}Source",
            name=source_name,
            log_type="TRACES",
            resource_arn=runtime.agent_runtime_arn,
        )
        return logs.CfnDelivery(
            self,
            f"{prefix}Delivery",
            delivery_source_name=source.ref,
            delivery_destination_arn=self.traces_destination.attr_arn,
        )

    def _add_baseline_grants(self, role: iam.Role, runtime_names: list[str]) -> None:
        """The agent's baseline that the Runtime L2 does not already add.

        The L2 grants logs, X-Ray, ``cloudwatch:PutMetricData`` (``bedrock-agentcore`` namespace)
        and the workload-identity token actions itself. Added here (the same set the retired
        toolkit runtime's auto-created ``BedrockAgentCoreRuntimeExecutionPolicy`` carried):

        - **Model invocation** — the agent's whole job (Converse/ConverseStream authorize as
          ``InvokeModel*``), plus guardrails; and Marketplace subscribe-on-first-call, only when
          Bedrock is the caller.
        - **AgentCore Identity** — API-key / OAuth2 token vault reads scoped to the workload
          identity of each runtime holding this role (``<name>-*``) and the vault's own secrets, plus
          ``sts:GetWebIdentityToken`` (AWS outbound JWT federation).

        Deliberately NOT carried over: the AWS-managed Code Interpreter grants (the harness has no
        Code Interpreter backend any more) and the toolkit's account-wide log *delivery* actions
        (CloudFormation, not the runtime, manages deliveries).
        """
        region, account = self.region, self.account
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModelInvocation",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ApplyGuardrail",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                    f"arn:aws:bedrock:{region}:{account}:*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="MarketplaceSubscribeOnFirstCall",
                actions=["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"],
                resources=["*"],
                conditions={"StringEquals": {"aws:CalledViaLast": "bedrock.amazonaws.com"}},
            )
        )
        directory = f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default"
        vault = f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default"
        own_identities = [f"{directory}/workload-identity/{name}-*" for name in runtime_names]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityTokenVault",
                actions=[
                    "bedrock-agentcore:GetResourceApiKey",
                    "bedrock-agentcore:GetResourceOauth2Token",
                ],
                resources=[
                    vault,
                    f"{vault}/apikeycredentialprovider/*",
                    f"{vault}/oauth2credentialprovider/*",
                    directory,
                    *own_identities,
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityProviderSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/oauth2/*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/apikey/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AwsJwtFederation", actions=["sts:GetWebIdentityToken"], resources=["*"]
            )
        )
