"""The retired agent runtime on AgentCore Runtime instances, kept as a rollback standby.

With ``-c agent_instances_standby=true`` this renders that runtime, its capacity provider and
roles exactly as deployed (same construct ids, names and properties), so CloudFormation leaves them
in place. Nothing invokes it. Everything here is frozen: the capacity provider is named and its
inputs are create-only (``tests/test_create_only_drift.py`` pins them). See docs/architecture.md.
"""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam

from .common import Naming

#: The generation the deployed capacity provider and runtime are named after (``…_agent_g2``).
AGENT_INSTANCES_GENERATION = 2
INSTANCE_TYPES = ("m7g.large", "m8g.large", "m6g.large")
ROOT_FREE_GIB = 20
IDLE_TIMEOUT_SECONDS = 3_600
MAX_LIFETIME_SECONDS = 1_209_600
OPERATOR_POLICY = "BedrockAgentCoreRuntimeInstancesOperatorRolePolicy"
INSTANCE_POLICY = "BedrockAgentCoreRuntimeInstancesInstanceRolePolicy"
STANDBY_DESCRIPTION = (
    "Strandly agent on Runtime instances: model, config secret, Memory, run ledger. Shell/file work "
    "runs in the separate sandbox runtime."
)


def standby_runtime_name(naming: Naming) -> str:
    return naming.agent_runtime_name(AGENT_INSTANCES_GENERATION)


def add_instances_standby(
    stack: Stack,
    *,
    naming: Naming,
    role: iam.IRole,
    artifact: agentcore.AgentRuntimeArtifact,
    entrypoint_environment: Mapping[str, str],
    subnets: list[str],
    security_group_id: str,
) -> tuple[agentcore.Runtime, agentcore.CfnCapacityProvider]:
    """Add the Instances runtime and its capacity provider to ``stack`` under their deployed ids."""
    operator = iam.Role(
        stack,
        "OperatorRole",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
        ),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(OPERATOR_POLICY)],
        description="AgentCore operates the agent capacity provider's EC2 with this role.",
    )
    instance_role = iam.Role(
        stack,
        "InstanceRole",
        role_name=naming.instance_role("agent"),
        assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(INSTANCE_POLICY)],
        description="Agent instance profile: AgentCore system logs only.",
    )
    profile = iam.CfnInstanceProfile(stack, "InstanceProfile", roles=[instance_role.role_name])

    cp = agentcore.CfnCapacityProvider(
        stack,
        "CapacityProvider",
        name=naming.agent_capacity_provider(AGENT_INSTANCES_GENERATION),
        description="Strandly agent on Runtime instances: one Graviton instance per session.",
        permissions_configuration=agentcore.CfnCapacityProvider.PermissionsConfigurationProperty(
            capacity_provider_operator_role_arn=operator.role_arn
        ),
        compute_configuration=agentcore.CfnCapacityProvider.ComputeConfigurationProperty(
            ec2_configuration=agentcore.CfnCapacityProvider.Ec2ConfigurationProperty(
                launch_template_source=agentcore.CfnCapacityProvider.LaunchTemplateSourceProperty(
                    launch_parameters=agentcore.CfnCapacityProvider.LaunchParametersProperty(
                        operating_system="LINUX_ARM64",
                        instance_requirements=agentcore.CfnCapacityProvider.InstanceRequirementsProperty(
                            allowed_instance_types=list(INSTANCE_TYPES)
                        ),
                        instance_profile_arn=profile.attr_arn,
                        propagated_tags={
                            "app": naming.name,
                            "env": naming.env,
                            "component": "agent",
                        },
                    )
                ),
                vpc_configuration=agentcore.CfnCapacityProvider.VpcConfigurationProperty(
                    subnets=subnets,
                    security_groups=[security_group_id],
                ),
                root_volume=agentcore.CfnCapacityProvider.RootVolumeConfigurationProperty(
                    free_space_gib=ROOT_FREE_GIB, volume_type="gp3", encrypted=True
                ),
                lifecycle_configuration=agentcore.CfnCapacityProvider.InstanceLifecycleConfigurationProperty(
                    idle_instance_timeout=IDLE_TIMEOUT_SECONDS,
                    max_lifetime=MAX_LIFETIME_SECONDS,
                ),
            )
        ),
    )
    cp.node.add_dependency(operator)

    runtime = agentcore.Runtime(
        stack,
        "Agent",
        runtime_name=standby_runtime_name(naming),
        agent_runtime_artifact=artifact,
        execution_role=role,
        description=STANDBY_DESCRIPTION,
        lifecycle_configuration=agentcore.LifecycleConfiguration(
            idle_runtime_session_timeout=Duration.seconds(IDLE_TIMEOUT_SECONDS),
            # The L2 validates against the microVM's 8 h; raised to 14 days on the L1 below.
            max_lifetime=Duration.hours(8),
        ),
        protocol_configuration=agentcore.ProtocolType.HTTP,
        environment_variables=dict(entrypoint_environment),
    )
    cfn_runtime = runtime.node.default_child
    if not isinstance(cfn_runtime, agentcore.CfnRuntime):  # pragma: no cover - defensive
        raise TypeError("Runtime.node.default_child is no longer a CfnRuntime")
    cfn_runtime.capacity_provider_configuration = (
        agentcore.CfnRuntime.CapacityProviderConfigurationProperty(capacity_provider_arn=cp.attr_arn)
    )
    cfn_runtime.add_property_override("LifecycleConfiguration.MaxLifetime", MAX_LIFETIME_SECONDS)
    # An Instances runtime takes its network from the capacity provider and rejects this property.
    cfn_runtime.add_property_deletion_override("NetworkConfiguration")
    return runtime, cp
