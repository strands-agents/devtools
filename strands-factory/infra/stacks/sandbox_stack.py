"""SandboxStack: the sandbox AgentCore Runtime on Runtime instances.

It owns the shared VPC (and the agent's security group), the EC2 capacity provider with a
per-session encrypted EBS workspace at ``/mnt/workspace``, the operator and instance roles, the
sandbox image, the runtime and the session-TTL scheduler group and role. The capacity provider's
compute configuration is create-only: change it by bumping ``common.SANDBOX_INSTANCES_GENERATION``.

The sandbox runs model-generated commands, so its execution role is the security boundary: it
gets no model, secret, session-store, ledger or memory permission of the agent's, and provider
federation is opt-in and audience-restricted. See docs/operations.md.
"""

from __future__ import annotations

from aws_cdk import Annotations, CfnDeletionPolicy, CfnOutput, Duration, Fn, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from .common import (
    MANAGED_BY_TAG_KEY,
    SANDBOX_IDLE_TIMEOUT_SECONDS,
    SANDBOX_MAX_LIFETIME_SECONDS,
    SANDBOX_MOUNT_PATH,
    Naming,
)
from .provider_federation import (
    PROVIDER_AUDIENCES,
    provider_federation_environment,
    validate_provider_federation,
)

#: The files ``Dockerfile.sandbox`` actually needs, expressed as an **allow-list** so the asset
#: hash depends on nothing else. The build context is ``sandbox/`` (the Dockerfile, its inputs and
#: the image-size gate), and a naive context would hash every file in it, so an unrelated change
#: there would rebuild and *redeploy the runtime*. CDK's exclude patterns
#: are gitignore-style, so ``"*"`` then ``"!<file>"`` re-includes: verified to stage exactly these
#: files, with an asset hash identical to the same files in an empty directory.
#: A deny-list was the obvious alternative and was rejected: it rots silently every time the repo
#: gains a directory, and the failure mode is invisible (a slow deploy, not an error).
SANDBOX_ASSET_INCLUDES = (
    "Dockerfile.sandbox",
    "sandbox_main.py",
    "requirements-sandbox.txt",
    "strandly_sandbox_auth.py",
    "strandly_sandbox_auth.mjs",
    "strandly_local_llm.sh",
)
SANDBOX_ASSET_EXCLUDE = ["*", *(f"!{name}" for name in SANDBOX_ASSET_INCLUDES)]

#: Instance types the SageMaker test grants may put in an endpoint config: single-GPU, cheapest tier.
SAGEMAKER_TEST_INSTANCE_TYPES = ("ml.g5.xlarge", "ml.g5.2xlarge", "ml.g6.xlarge", "ml.g6.2xlarge")
#: AWS Deep Learning Containers registry for every non-opt-in commercial region.
DLC_REGISTRY_ACCOUNT = "763104351884"

#: The sandbox runtime's description, carried over from the imperative deployer it replaces.
SANDBOX_DESCRIPTION = (
    "Strandly sandbox: the shell + filesystem the agent's bash/read/write/edit tools run in, on "
    "Runtime instances with a persistent EBS workspace per session. No agent, no credentials, "
    "minimal execution role."
)

#: arm64 (the sandbox image is arm64-only), 4–8 vCPU / 16 GiB.
INSTANCE_TYPES = ("m7g.xlarge", "m8g.xlarge", "c7g.2xlarge")
#: The per-session persistent workspace volume, mounted at ``SANDBOX_MOUNT_PATH``.
WORKSPACE_GIB = 40
WORKSPACE_VOLUME = "workspace"
#: Free space on the root volume (``/tmp`` and the image; the workspace and caches are on the volume).
ROOT_FREE_GIB = 30
#: An instance stops after 1 h idle (its volume is kept); an instance lives at most 14 days.
IDLE_INSTANCE_TIMEOUT_SECONDS = 3_600
INSTANCE_MAX_LIFETIME_SECONDS = 1_209_600
#: The service's lifecycle bounds (CloudFormation registry schema, Runtime and CapacityProvider).
LIFECYCLE_MIN_SECONDS = 60
LIFECYCLE_MAX_SECONDS = 1_209_600
OPERATOR_POLICY = "BedrockAgentCoreRuntimeInstancesOperatorRolePolicy"
INSTANCE_POLICY = "BedrockAgentCoreRuntimeInstancesInstanceRolePolicy"
#: Cut-over safety for the release that moves the sandbox from the microVM runtime
#: (``<name>_<env>_sandbox``) to Runtime instances (``<name>_<env>_sandbox_ec2``): the rename makes
#: CloudFormation REPLACE the runtime, and with this on the replaced microVM runtime is RETAINED
#: (``UpdateReplacePolicy: Retain``) instead of deleted — the still-live toolkit agent keeps its
#: sandbox until the same Deploy run wires the new agent. The old runtime is then deleted by hand
#: (the cut-over). TURN THIS OFF in the release after the cut-over: left on, a later
#: ``SANDBOX_INSTANCES_GENERATION`` bump would retain the old ``_ec2`` runtime too, and its capacity
#: provider could then not be deleted (a provider still referenced by a runtime cannot be).
RETAIN_REPLACED_SANDBOX_RUNTIME = True
#: The ``Runtime`` L2 rejects a ``maxLifetime`` above 8 h; the L1 override carries the
#: real (up to 14-day) value.
_L2_MAX_LIFETIME_SECONDS = 28_800


def validate_mount_path(mount_path: str) -> None:
    """Reject a volume ``MountPath`` the service would reject, at synth time.

    The service requires ``/mnt/<single-directory>`` (schema pattern ``^/mnt/[a-zA-Z0-9._-]+/?$``)
    and answers anything else with a ``ValidationException``. Failing in ``cdk synth`` — before a
    single API call — is strictly better than failing partway through a CloudFormation deploy, which
    is where the L1 escape hatch would otherwise surface it.

    Raises:
        ValueError: If ``mount_path`` is not ``/mnt/<one-level>``.
    """
    parts = mount_path.strip("/").split("/")
    if not mount_path.startswith("/mnt/") or len(parts) != 2 or not parts[1]:
        raise ValueError(
            f"the workspace mountPath must be /mnt/<single-directory> (got {mount_path!r}); "
            "the service rejects anything else."
        )


class SandboxStack(Stack):
    """The sandbox runtime on Runtime instances: VPC, capacity provider, ARM64 image asset, the
    near-powerless execution role, and the session-TTL Scheduler group + role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        asset_directory: str,
        runtime_name: str | None = None,
        mount_path: str = SANDBOX_MOUNT_PATH,
        idle_timeout_seconds: int = SANDBOX_IDLE_TIMEOUT_SECONDS,
        max_lifetime_seconds: int = SANDBOX_MAX_LIFETIME_SECONDS,
        bedrock_test_grants: bool = True,
        sagemaker_test_grants: bool = False,
        assumable_role_arns: tuple[str, ...] = (),
        provider_federation: dict[str, dict[str, str]] | None = None,
        workspace_gib: int = WORKSPACE_GIB,
        **kwargs: object,
    ) -> None:
        """Create the sandbox runtime.

        Args:
            naming: Deployment naming (name + env suffix).
            asset_directory: The Docker build context — the ``sandbox/`` folder, which is where
                ``Dockerfile.sandbox`` and its inputs live. Only
                :data:`SANDBOX_ASSET_INCLUDES` is actually staged.
            runtime_name: Override the per-env runtime name (default ``naming.sandbox_runtime``).
            mount_path: Where the per-session EBS volume is mounted. Must be ``/mnt/<one-level>``.
            idle_timeout_seconds: ``idleRuntimeSessionTimeout``. Must be <= ``max_lifetime_seconds``.
            max_lifetime_seconds: The runtime's ``maxLifetime`` (60 s – 14 days).
            sagemaker_test_grants: Opt in to ABAC-scoped SageMaker endpoint lifecycle for live
                ``SageMakerAIModel`` tests (see :meth:`_add_sagemaker_test_grants`). Off by default:
                GPU endpoints bill hourly.
            assumable_role_arns: Cross-account roles the sandbox may ``sts:AssumeRole`` into
                (see :meth:`_add_assume_role_grants`). Empty by default; exact role ARNs are operator-selected.
            provider_federation: Public Anthropic/OpenAI/Google federation identifiers. Disabled by
                default (equivalent to an empty dict); Anthropic/OpenAI get only audience/TTL-scoped
                STS identity-token issuance, Google gets no grant. Account enablement and provider
                registration stay manual.
            workspace_gib: Size of the per-session EBS workspace volume (30–50 GiB). Part of the
                capacity provider's create-only compute configuration: changing it needs a
                ``SANDBOX_INSTANCES_GENERATION`` bump.

        Raises:
            ValueError: On invalid provider federation, an invalid mount path, a lifecycle outside
                the service bounds or an idle timeout above the max lifetime, or a workspace size
                outside 30–50 GiB.
        """
        super().__init__(scope, construct_id, **kwargs)

        federation = validate_provider_federation(
            {} if provider_federation is None else provider_federation
        )
        validate_mount_path(mount_path)
        if idle_timeout_seconds > max_lifetime_seconds:
            # Same guard the imperative deployer had. The service enforces it; catching it here
            # keeps the error a one-line synth failure instead of a rolled-back stack.
            raise ValueError(
                f"idleRuntimeSessionTimeout ({idle_timeout_seconds}s) must be <= maxLifetime "
                f"({max_lifetime_seconds}s)."
            )
        for label, value in (
            ("idleRuntimeSessionTimeout", idle_timeout_seconds),
            ("maxLifetime", max_lifetime_seconds),
        ):
            if not LIFECYCLE_MIN_SECONDS <= value <= LIFECYCLE_MAX_SECONDS:
                raise ValueError(
                    f"{label} must be {LIFECYCLE_MIN_SECONDS}–{LIFECYCLE_MAX_SECONDS}s (got {value})."
                )
        if not 30 <= workspace_gib <= 50:
            raise ValueError(f"the workspace volume should be 30–50 GiB (got {workspace_gib})")

        name = runtime_name or naming.sandbox_runtime
        sandbox_prefix = f"{naming.under}_sandbox"
        trust_prefix = sandbox_prefix if name.startswith(sandbox_prefix) else name

        role = iam.Role(
            self,
            "SandboxExecRole",
            # NOT `-sandbox-exec`: accounts that ran the former ``Strandly-SandboxIam-<env>`` stack
            # may still hold a role of that name, and CloudFormation does not adopt an existing
            # role (``EntityAlreadyExists`` rolls the deploy back). A distinct name never collides.
            role_name=f"{naming.hyphen}-sandbox-runtime-exec",
            # AgentCore Runtime assumes the role on the sandbox's behalf. The two condition keys are
            # standard confused-deputy protection: only this account's resources, and only a runtime
            # whose ARN starts with this sandbox's name, may use it — so the role cannot be borrowed
            # by another account's runtime, or by the *agent* runtime in this one, even if its ARN
            # leaked. Scoping to the name (rather than the full ARN) avoids the circular dependency
            # of needing the runtime's ARN to build the role the runtime needs; the trailing `*`
            # covers the service-generated id suffix.
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            # `<name>_<env>_sandbox*`, not `{name}*`: it must keep matching the
                            # retired microVM `<name>_<env>_sandbox` (retained across the cut-over,
                            # RETAIN_REPLACED_SANDBOX_RUNTIME) as well as `…_sandbox_ec2[_g<n>]`.
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/"
                            f"{trust_prefix}*"
                        )
                    },
                },
            ),
            description=(
                "Execution role for the Strandly SANDBOX runtime. Minimal by default: image pull "
                "+ write-only logs/metrics/traces. No Secrets Manager, no Memory, no "
                "DynamoDB, no InvokeAgentRuntime, no log reads; Bedrock/KB only via the opt-out "
                "test grants."
            ),
        )

        vpc, security_group = self._network()
        agent_security_group = self._agent_network(vpc)
        capacity_provider = self._capacity_provider(naming, vpc, security_group, workspace_gib)

        runtime = agentcore.Runtime(
            self,
            # The logical id the microVM runtime had: with the new name this is a replacement.
            "Sandbox",
            runtime_name=name,
            # Builds Dockerfile.sandbox for arm64 (the capacity provider is LINUX_ARM64) and pushes
            # it to the bootstrap assets repo. The L2 scopes this role's ECR pull to that repository.
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                asset_directory,
                file="Dockerfile.sandbox",
                platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=SANDBOX_ASSET_EXCLUDE,
                display_name=f"{name} image",
            ),
            execution_role=role,
            description=SANDBOX_DESCRIPTION,
            # Idle raised well above the 900s default: a long autonomous turn can legitimately go
            # minutes between shell calls, and losing the box mid-turn cost us a run at ~910s once.
            # The L2 caps maxLifetime at 8 h; the real value (up to 14 days on instances) is set on
            # the L1 in _attach_capacity_provider.
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.seconds(idle_timeout_seconds),
                max_lifetime=Duration.seconds(min(max_lifetime_seconds, _L2_MAX_LIFETIME_SECONDS)),
            ),
            # Explicit, though it matches the service default: sandbox_main.py serves the AgentCore
            # HTTP contract (/invocations + /ping), and this is load-bearing enough to state.
            protocol_configuration=agentcore.ProtocolType.HTTP,
            # No runtime env by default; opt-in federation adds only public provider IDs. No secret
            # ARN, provider key, model config, or GitHub token (git credentials arrive per session).
            # Tokens are obtained on demand via the sandbox execution role, never baked in here.
            environment_variables=provider_federation_environment(federation) or None,
        )
        # Egress: the instances sit in the private subnets behind the one NAT gateway, with an
        # egress-only security group. The agent's whole job needs egress — `git clone`, `pip
        # install`, `npm ci` — so this is the one place the sandbox is deliberately *not* locked
        # down. Egress is how exfiltration would happen; the mitigation is that there is little in
        # this box worth exfiltrating (no static secret; the execution role's data-plane grants are
        # explicit — opt-out Bedrock test grants, opt-in assume-role and provider
        # federation) plus CloudWatch/CloudTrail auditing of every command. A deployment
        # that needs egress control puts an egress proxy (or a firewall) on that NAT route.
        self._attach_capacity_provider(
            runtime, capacity_provider, mount_path, max_lifetime_seconds
        )
        if RETAIN_REPLACED_SANDBOX_RUNTIME:
            self._retain_on_replace(runtime)
        self.ttl_group, self.ttl_role = self._session_ttl(naming, capacity_provider)
        self._add_assume_role_grants(role, assumable_role_arns)
        if federation.keys() & PROVIDER_AUDIENCES.keys():
            self._add_provider_federation_grant(role, federation)
        if bedrock_test_grants:
            self._add_bedrock_test_grants(role, naming)
        if sagemaker_test_grants:
            self._add_sagemaker_test_grants(role, naming)

        self.role = role
        self.runtime = runtime
        self.vpc = vpc
        self.agent_security_group = agent_security_group
        self.capacity_provider = capacity_provider
        CfnOutput(
            self,
            "SandboxRuntimeArn",
            value=runtime.agent_runtime_arn,
            description=(
                "The sandbox runtime ARN — becomes STRANDLY_SANDBOX_RUNTIME_ARN on the agent "
                "runtime. Read from this output by your deploy pipeline; never copied by "
                "hand."
            ),
        )
        CfnOutput(
            self,
            "SandboxExecRoleArn",
            value=role.role_arn,
            description="The sandbox's own execution role (never the agent's).",
        )
        CfnOutput(self, "CapacityProviderArn", value=capacity_provider.attr_arn)
        # The sliding session TTL's deploy wiring: `strandly provision` exports these three as
        # STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID / _TTL_SCHEDULE_GROUP / _TTL_ROLE_ARN for the AGENT
        # runtime (sandbox/ttl.py runs there), exactly like SandboxRuntimeArn.
        CfnOutput(
            self,
            "CapacityProviderId",
            value=capacity_provider.attr_capacity_provider_id,
            description="Becomes STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID on the agent runtime.",
        )
        CfnOutput(
            self,
            "SandboxTtlScheduleGroup",
            value=self.ttl_group.ref,
            description="Becomes STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP on the agent runtime.",
        )
        CfnOutput(
            self,
            "SandboxTtlSchedulerRoleArn",
            value=self.ttl_role.role_arn,
            description="Becomes STRANDLY_SANDBOX_TTL_ROLE_ARN on the agent runtime.",
        )
        # The shared VPC's hand-over to the AGENT runtime (AgentRuntimeStack's -c agent_subnet_ids /
        # -c agent_security_group_id): `strandly provision` exports them as
        # STRANDLY_AGENT_SUBNET_IDS / STRANDLY_AGENT_SECURITY_GROUP_ID, `strandly deploy` passes
        # them as context. Plain outputs, no Fn::ImportValue (the repo avoids cross-stack exports:
        # an export pins its producer until every importer is gone). Reading them after this stack
        # completes is also what orders the agent's capacity provider after the NAT route.
        CfnOutput(
            self,
            "AgentSubnetIds",
            value=Fn.join(",", [subnet.subnet_id for subnet in vpc.private_subnets]),
            description="Becomes -c agent_subnet_ids (comma-joined private subnets, behind the NAT).",
        )
        CfnOutput(
            self,
            "AgentSecurityGroupId",
            value=agent_security_group.security_group_id,
            description="Becomes -c agent_security_group_id (the agent's egress-only SG).",
        )

    def _add_provider_federation_grant(
        self, role: iam.Role, federation: dict[str, dict[str, str]]
    ) -> None:
        """Only short-lived identity-token issuance; no account enablement or provider secrets.

        GetWebIdentityToken has no resource-level scope. The audience array must be a subset of
        enabled providers (not merely contain one allowed audience), with a five-minute ceiling.
        All three condition keys, including sts:SigningAlgorithm, are documented at:
        https://docs.aws.amazon.com/service-authorization/latest/reference/list_sts.html
        """
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SandboxProviderFederation",
                actions=["sts:GetWebIdentityToken"],
                resources=["*"],
                conditions={
                    "ForAllValues:StringEquals": {
                        "sts:IdentityTokenAudience": [
                            PROVIDER_AUDIENCES[p] for p in federation if p in PROVIDER_AUDIENCES
                        ]
                    },
                    "NumericLessThanEquals": {"sts:DurationSeconds": 300},
                    "StringEquals": {"sts:SigningAlgorithm": "RS256"},
                },
            )
        )

    def _add_assume_role_grants(self, role: iam.Role, role_arns: tuple[str, ...]) -> None:
        """``sts:AssumeRole`` on an explicit list of roles in OTHER accounts — nothing else.

        This is how the sandbox reaches data that lives outside this account without any of that
        data's permissions ever being granted here. The target role owns its trust and data
        permissions; this stack grants only sts:AssumeRole to exact operator-supplied ARNs.
        """
        if not role_arns:
            return
        for arn in role_arns:
            if "*" in arn or not arn.startswith("arn:aws:iam::"):
                raise ValueError(f"assumable_role_arns must be exact IAM role ARNs, got {arn!r}")
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeExternalRoles",
                actions=["sts:AssumeRole"],
                resources=list(role_arns),
            )
        )

    def _add_bedrock_test_grants(self, role: iam.Role, naming: Naming) -> None:
        """Let the sandbox e2e-test Strands against live Bedrock.

        **The one deliberate hole in "the sandbox role can start a container and nothing else".**
        These are the statements of ``backend_stack._ci_execution_role``, plus a read-only
        ``ListInferenceProfiles``, so the agent can run Strands' own Bedrock tests.

        Credentials are reachable by — and exfiltratable from — anything the agent runs, so
        everything below is bounded by two guards rather than trust:

        - **Invoke-only on models.** No Bedrock control plane for inference: scoped to
          ``foundation-model/*`` + ``inference-profile/*``, so any Strands test model works without a
          role edit, but models cannot be managed. Listing models and inference profiles is
          read-only.
        - **An ABAC tag boundary on lifecycle.** It may *create* KB / guardrail / data-source /
          bucket resources only while tagging them ``ManagedBy=strandly``, and may read/update/delete
          only resources already tagged so. Prod backends are tagged ``strandly-infra``, so they are
          invisible to these grants — the agent cannot reach its own production KB/Memory/runtime.

        Deliberately still withheld: ``secretsmanager:GetSecretValue`` (the config secret holds the
        real GitHub token and every backend id), ``iam:CreateRole``/``CreatePolicy`` (role creation
        from a sandbox is a privilege-escalation hole — a KB needs a service role, so ONE fixed role
        is pre-created and ``iam:PassRole`` is granted on only that ARN), Memory/Event actions, the
        run-ledger table, and ``InvokeAgentRuntime*``.

        Pass ``bedrock_test_grants=False`` (``-c sandbox_bedrock_grants=false``) for a
        credential-free sandbox — the right choice for a throwaway env, and the reason this is a knob
        at all.
        """
        agent_tag = {f"aws:RequestTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}
        resource_tag = {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}

        # 0. CLOSE THE RE-TAG ESCALATION — the load-bearing guard for the whole self-protection
        #    claim. Without it the ABAC boundary is bypassable: ListKnowledgeBases → find a prod KB
        #    (ManagedBy=strandly-infra) → TagResource it to ManagedBy=strandly (tagging overwrites by
        #    key, satisfying statement 2's RequestTag gate) → then Delete/Retrieve it through
        #    statement 3's ResourceTag gate. An explicit Deny always beats an Allow, so denying
        #    (Un)TagResource on anything already tagged strandly-infra makes prod un-re-taggable
        #    while still allowing freshly-created untagged resources to be tagged.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyRetagInfra",
                effect=iam.Effect.DENY,
                actions=["bedrock:TagResource", "bedrock:UntagResource"],
                resources=["*"],
                conditions={
                    "StringEquals": {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.infra_tag}
                },
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
        # 2. Create lifecycle — only while tagging the new resource ManagedBy=strandly. Create* has
        #    no pre-existing ARN, so Resource is * and the RequestTag condition is the gate (the
        #    standard ABAC create pattern). aws:TagKeys is pinned so the request may carry ONLY the
        #    ManagedBy key — no smuggling extra tags alongside it.
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
        # Read-only discovery of the profile ids BedrockInvoke accepts; it invokes, creates, and
        # deletes nothing. The action takes both profile resource types (Service Authorization
        # Reference), scoped like BedrockInvoke: this account, any region.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListInferenceProfiles",
                actions=["bedrock:ListInferenceProfiles"],
                resources=[
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    f"arn:aws:bedrock:*:{self.account}:application-inference-profile/*",
                ],
            )
        )
        # 4. A fixed KB service role the agent PASSES to CreateKnowledgeBase (never creates its own).
        #    Physical name differs from the Backend stack's `<name>_managed_kb_role`: two stacks
        #    cannot own one role name, and CloudFormation does not adopt a pre-existing resource.
        kb_test_role = iam.Role(
            self,
            "SandboxManagedKbRole",
            role_name=f"{naming.under}_sandbox_managed_kb_role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description=(
                "Service role the sandbox passes to CreateKnowledgeBase for ManagedBy=strandly "
                "test KBs."
            ),
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
        # 5. S3 for test media + KB sources — name-scoped to strandly-managed-*, defence in depth
        #    alongside the tag gate.
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

    def _add_sagemaker_test_grants(self, role: iam.Role, naming: Naming) -> None:
        """Let the sandbox create, invoke and delete its own SageMaker endpoints, nothing else.

        Opt-in (``-c sandbox_sagemaker_grants=true``) because a leaked GPU endpoint bills by the
        hour. Same ABAC boundary as :meth:`_add_bedrock_test_grants`, plus name scoping: models,
        endpoint configs and endpoints must be named ``strandly-managed-*`` and tagged
        ``ManagedBy=strandly`` at creation, and only those can be described, invoked or deleted.

        - **No re-tag escalation.** ``sagemaker:AddTags`` is allowed only with
          ``sagemaker:TaggingAction`` set to one of the three create calls, so a direct AddTags
          cannot pull an existing endpoint into the boundary. ``DeleteTags`` is never allowed, and
          ``strandly-infra`` resources are denied both outright.
        - **Bounded spend and content.** Endpoint configs may use only
          :data:`SAGEMAKER_TEST_INSTANCE_TYPES` and only ``strandly-managed-*`` models. The one
          passable execution role pulls only the DLC ``djl-inference`` image and writes only
          endpoint logs: no S3, so weights come from the public Hugging Face Hub.
        """
        managed = naming.managed_prefix + "-*"
        arn = f"arn:aws:sagemaker:*:{self.account}"
        model, config, endpoint = (
            f"{arn}:model/{managed}",
            f"{arn}:endpoint-config/{managed}",
            f"{arn}:endpoint/{managed}",
        )
        request_tag = {f"aws:RequestTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}
        tag_keys = {"aws:TagKeys": [MANAGED_BY_TAG_KEY]}
        tag_on_create = {"StringEquals": request_tag, "ForAllValues:StringEquals": tag_keys}

        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenySageMakerRetagInfra",
                effect=iam.Effect.DENY,
                actions=["sagemaker:AddTags", "sagemaker:DeleteTags"],
                resources=["*"],
                conditions={
                    "StringEquals": {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.infra_tag}
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerTagOnCreate",
                actions=["sagemaker:AddTags"],
                resources=[model, config, endpoint],
                conditions={
                    "StringEquals": {
                        **request_tag,
                        "sagemaker:TaggingAction": [
                            "CreateModel",
                            "CreateEndpointConfig",
                            "CreateEndpoint",
                        ],
                    },
                    "ForAllValues:StringEquals": tag_keys,
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerCreateModel",
                actions=["sagemaker:CreateModel"],
                resources=[model],
                conditions=tag_on_create,
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerCreateEndpointConfig",
                actions=["sagemaker:CreateEndpointConfig"],
                resources=[config],
                conditions={
                    "StringEquals": request_tag,
                    "ForAllValues:StringEquals": {
                        **tag_keys,
                        "sagemaker:InstanceTypes": list(SAGEMAKER_TEST_INSTANCE_TYPES),
                    },
                    # ForAllValues passes on an absent key; require instance types to be present.
                    "Null": {"sagemaker:InstanceTypes": "false"},
                    "ArnLike": {"sagemaker:ModelArn": model},
                },
            )
        )
        # CreateEndpoint authorizes both the new endpoint and the config it is built from.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerCreateEndpoint",
                actions=["sagemaker:CreateEndpoint"],
                resources=[endpoint, config],
                conditions=tag_on_create,
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerOperateManaged",
                actions=[
                    "sagemaker:InvokeEndpoint",
                    "sagemaker:InvokeEndpointWithResponseStream",
                    "sagemaker:DescribeModel",
                    "sagemaker:DescribeEndpointConfig",
                    "sagemaker:DescribeEndpoint",
                    "sagemaker:DeleteModel",
                    "sagemaker:DeleteEndpointConfig",
                    "sagemaker:DeleteEndpoint",
                ],
                resources=[model, config, endpoint],
                conditions={
                    "StringEquals": {f"aws:ResourceTag/{MANAGED_BY_TAG_KEY}": naming.agent_tag}
                },
            )
        )
        # List* has no resource type; read-only, needed to find leftovers.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerList",
                actions=[
                    "sagemaker:ListModels",
                    "sagemaker:ListEndpointConfigs",
                    "sagemaker:ListEndpoints",
                ],
                resources=["*"],
            )
        )

        exec_role = iam.Role(
            self,
            "SandboxManagedSageMakerRole",
            role_name=f"{naming.under}_sandbox_managed_sagemaker_role",
            assumed_by=iam.ServicePrincipal(
                "sagemaker.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description=(
                "Execution role the sandbox passes to CreateModel for ManagedBy=strandly test "
                "endpoints: DLC image pull and endpoint logs only."
            ),
        )
        # GetAuthorizationToken has no resource type.
        exec_role.add_to_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[f"arn:aws:ecr:*:{DLC_REGISTRY_ACCOUNT}:repository/djl-inference"],
            )
        )
        log_group = f"arn:aws:logs:*:{self.account}:log-group:/aws/sagemaker/Endpoints/*"
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[log_group],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group}:log-stream:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassSageMakerExecRole",
                actions=["iam:PassRole"],
                resources=[exec_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}},
            )
        )

    def _network(self) -> tuple[ec2.Vpc, ec2.SecurityGroup]:
        """The deployment's ONE VPC (2 AZs, ONE NAT gateway) and the sandbox's egress-only SG.

        This VPC is shared: the AGENT capacity provider (AgentRuntimeStack) launches into the same
        private subnets with its own security group (:meth:`_agent_network`), handed over through
        the ``AgentSubnetIds`` / ``AgentSecurityGroupId`` outputs. One VPC per environment, not one
        per runtime: the account's VPC quota is 5, and two environments × two VPCs + the default
        VPC would be 5/5 — no headroom for anything else (and two NATs/EIPs per env for no benefit).
        """
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            # One NAT, not one per AZ: neither runtime is an HA service (a session is one instance),
            # and a second NAT gateway would double the fixed cost (~$33/month each).
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            ],
            # Free gateway endpoints keep S3 (ECR image layers, the agent's session snapshots) and
            # DynamoDB (the run ledger) off the metered NAT.
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3),
                "DynamoDb": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
                ),
            },
        )
        security_group = ec2.SecurityGroup(
            self,
            "SandboxSg",
            vpc=vpc,
            allow_all_outbound=True,
            description="Strandly sandbox instances: egress only (git/pip/npm), no ingress.",
        )
        return vpc, security_group

    def _agent_network(self, vpc: ec2.Vpc) -> ec2.SecurityGroup:
        """The AGENT capacity provider's egress-only security group, in this stack's VPC.

        Separate from the sandbox's: the two runtimes share subnets, not a security group, so
        neither can ever reach the other's instances (no ingress rules on either). Owned here, not
        in AgentRuntimeStack, because the VPC is — AgentRuntimeStack takes its id (and the private
        subnet ids) as CDK context via ``strandly provision`` → ``strandly deploy``, never through a
        CloudFormation export.
        """
        return ec2.SecurityGroup(
            self,
            "AgentSg",
            vpc=vpc,
            allow_all_outbound=True,
            description=(
                "Strandly agent instances: egress only (Bedrock, AgentCore, GitHub, Slack, S3)."
            ),
        )

    def _capacity_provider(
        self,
        naming: Naming,
        vpc: ec2.Vpc,
        security_group: ec2.SecurityGroup,
        workspace_gib: int,
    ) -> agentcore.CfnCapacityProvider:
        """The EC2 capacity provider: arm64 instances, a per-session EBS workspace, cost tags."""
        operator = iam.Role(
            self,
            "OperatorRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(OPERATOR_POLICY)],
            description="AgentCore operates the Strandly sandbox capacity provider's EC2 with this.",
        )
        instance_role = iam.Role(
            self,
            "InstanceRole",
            role_name=naming.instance_role("sandbox"),
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(INSTANCE_POLICY)],
            description="Strandly sandbox instance profile: AgentCore system logs only.",
        )
        profile = iam.CfnInstanceProfile(self, "InstanceProfile", roles=[instance_role.role_name])

        cfn = agentcore.CfnCapacityProvider
        provider = cfn(
            self,
            "CapacityProvider",
            name=naming.sandbox_capacity_provider,
            description="Strandly sandbox on Runtime instances: per-session EC2 + EBS workspace.",
            permissions_configuration=cfn.PermissionsConfigurationProperty(
                capacity_provider_operator_role_arn=operator.role_arn
            ),
            compute_configuration=cfn.ComputeConfigurationProperty(
                ec2_configuration=cfn.Ec2ConfigurationProperty(
                    launch_template_source=cfn.LaunchTemplateSourceProperty(
                        launch_parameters=cfn.LaunchParametersProperty(
                            operating_system="LINUX_ARM64",
                            instance_requirements=cfn.InstanceRequirementsProperty(
                                allowed_instance_types=list(INSTANCE_TYPES)
                            ),
                            instance_profile_arn=profile.attr_arn,
                            # The instances/volumes/ENIs AgentCore launches are not CloudFormation
                            # resources, so the app-wide cost tags never reach them otherwise.
                            propagated_tags={
                                "app": naming.name,
                                "env": naming.env,
                                "component": "sandbox",
                            },
                        )
                    ),
                    vpc_configuration=cfn.VpcConfigurationProperty(
                        subnets=[subnet.subnet_id for subnet in vpc.private_subnets],
                        security_groups=[security_group.security_group_id],
                    ),
                    root_volume=cfn.RootVolumeConfigurationProperty(
                        free_space_gib=ROOT_FREE_GIB, volume_type="gp3", encrypted=True
                    ),
                    volumes=[
                        cfn.VolumeConfigurationProperty(
                            ebs_configuration=cfn.EbsVolumeConfigurationProperty(
                                name=WORKSPACE_VOLUME,
                                size_gib=workspace_gib,
                                volume_type="gp3",
                                encrypted=True,
                            )
                        )
                    ],
                    lifecycle_configuration=cfn.InstanceLifecycleConfigurationProperty(
                        idle_instance_timeout=IDLE_INSTANCE_TIMEOUT_SECONDS,
                        max_lifetime=INSTANCE_MAX_LIFETIME_SECONDS,
                    ),
                )
            ),
        )
        # CloudFormation must not create the provider before AgentCore can assume the operator
        # role, nor before the private subnets can reach the internet (the NAT route).
        provider.node.add_dependency(operator)
        # (`vpc.internet_connectivity_established` covers only the IGW; each PRIVATE subnet's own
        # covers its 0.0.0.0/0 route through the NAT, which is what the instances egress through.)
        for subnet in vpc.private_subnets:
            provider.node.add_dependency(subnet.internet_connectivity_established)
        return provider

    @staticmethod
    def _retain_on_replace(runtime: agentcore.Runtime) -> None:
        """Keep the REPLACED sandbox runtime alive (see :data:`RETAIN_REPLACED_SANDBOX_RUNTIME`).

        ``UpdateReplacePolicy: Retain`` makes CloudFormation skip the delete of the old physical
        runtime when a name change replaces it, so the microVM ``<name>_<env>_sandbox`` the still-live
        toolkit agent points at keeps serving until the Deploy run re-points the agent. Only the
        replace policy: ``DeletionPolicy`` stays ``Delete`` so tearing the stack down still removes
        the CURRENT runtime (which must go before its capacity provider can).
        """
        cfn_runtime = runtime.node.default_child
        if not isinstance(cfn_runtime, agentcore.CfnRuntime):  # pragma: no cover - defensive
            raise TypeError("Runtime.node.default_child is no longer a CfnRuntime")
        cfn_runtime.cfn_options.update_replace_policy = CfnDeletionPolicy.RETAIN
        Annotations.of(cfn_runtime).acknowledge_warning(
            "CloudFormation-Validate::W3011",
            "Retain on REPLACE only, deliberately: a stack teardown must still delete the current "
            "runtime (before its capacity provider).",
        )

    @staticmethod
    def _attach_capacity_provider(
        runtime: agentcore.Runtime,
        capacity_provider: agentcore.CfnCapacityProvider,
        mount_path: str,
        max_lifetime_seconds: int,
    ) -> None:
        """Put the runtime on the capacity provider, with the per-session volume at ``mount_path``.

        Neither ``capacityProviderConfiguration`` nor ``filesystemConfigurations`` is exposed by the
        ``Runtime`` L2 (aws-cdk-lib 2.275), so both go on the generated ``CfnRuntime``. Three more L1
        edits: the real ``MaxLifetime`` (the L2 caps it at 8 h; instances sessions
        live up to 14 days), and deleting ``NetworkConfiguration``, which the L2 always renders and
        an instances runtime rejects ("Do not pass networkConfiguration when you specify
        capacityProviderConfiguration" — networking comes from the capacity provider). There is no
        ``SessionStorage`` and no ``PlatformVersion``: the workspace is the EBS volume.
        """
        cfn_runtime = runtime.node.default_child
        if not isinstance(cfn_runtime, agentcore.CfnRuntime):  # pragma: no cover - defensive
            # If a future aws-cdk-lib changes the L2's child shape, fail loudly at synth: silently
            # skipping this would deploy a sandbox with an *ephemeral* filesystem.
            raise TypeError(
                "Runtime.node.default_child is no longer a CfnRuntime "
                f"(got {type(cfn_runtime).__name__}); the capacity-provider escape hatch needs "
                "updating — check whether the L2 now exposes these properties directly."
            )
        cfn_runtime.capacity_provider_configuration = (
            agentcore.CfnRuntime.CapacityProviderConfigurationProperty(
                capacity_provider_arn=capacity_provider.attr_arn
            )
        )
        cfn_runtime.filesystem_configurations = [
            agentcore.CfnRuntime.FilesystemConfigurationProperty(
                capacity_provider_volume=agentcore.CfnRuntime.CapacityProviderVolumeConfigurationProperty(
                    volume_name=WORKSPACE_VOLUME, mount_path=mount_path
                )
            )
        ]
        cfn_runtime.add_property_override("LifecycleConfiguration.MaxLifetime", max_lifetime_seconds)
        cfn_runtime.add_property_deletion_override("NetworkConfiguration")

    def _session_ttl(
        self, naming: Naming, capacity_provider: agentcore.CfnCapacityProvider
    ) -> tuple[scheduler.CfnScheduleGroup, iam.Role]:
        """The Scheduler group the harness's per-session TTL schedules live in, and the role their
        universal target (``aws-sdk:bedrockagentcore:deleteCapacityProviderSession``) runs as.

        The role may call exactly ``DeleteCapacityProviderSession`` on this capacity provider (and
        its sessions), and trusts Scheduler for this account only. The agent may *pass* it (scoped
        in RuntimeIamStack) but never holds the delete permission itself.
        """
        group = scheduler.CfnScheduleGroup(
            self, "SessionTtlGroup", name=naming.sandbox_ttl_schedule_group
        )
        role = iam.Role(
            self,
            "SessionTtlSchedulerRole",
            role_name=naming.sandbox_ttl_scheduler_role,
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="EventBridge Scheduler deletes idle Strandly sandbox sessions (EBS TTL).",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DeleteSandboxSessions",
                actions=["bedrock-agentcore:DeleteCapacityProviderSession"],
                resources=[capacity_provider.attr_arn, f"{capacity_provider.attr_arn}/*"],
            )
        )
        return group, role
