"""OidcStack: GitHub Actions OIDC federation for deploying and invoking the agent.

Two roles because the blast radii differ: the deploy role (``<name>-<env>-gha-deploy``) runs
``cdk deploy`` and trusts only the repository's protected refs; the invoke role
(``<name>-<env>-gha-invoke``) may only invoke the agent runtime (never the sandbox runtime) and
trusts only the default branch ``main``. Trust policies pin the immutable ``owner@id/repo@id`` subject form. The OIDC provider is an account-wide singleton:
pass ``-c oidc_provider_arn`` to import it.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from .common import Naming

# The GitHub Actions OIDC issuer. Same value for every repo/account.
GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOSTNAME = "token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"


class OidcStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        github_repo: str,
        deploy_subjects: list[str] | None = None,
        invoke_subjects: list[str] | None = None,
        extra_invoke_repos: list[str] | None = None,
        oidc_provider_arn: str | None = None,
        runtime_arn: str | None = None,
        deploy_policy: str = "scoped",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- 1. The OIDC identity provider (create, or import an existing account-global one) -----
        if oidc_provider_arn:
            provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self, "GitHubOidc", oidc_provider_arn
            )
        else:
            provider = iam.OpenIdConnectProvider(
                self,
                "GitHubOidc",
                url=GITHUB_OIDC_URL,
                client_ids=[GITHUB_OIDC_AUDIENCE],
            )

        # --- 2. Default subject (``sub``) claims --------------------------------------------------
        # Deploy is privileged → default to protected refs only (merged main + this deployment's
        # GitHub Environment). Invoke is lower-risk but still defaults to main; widen explicitly.
        #
        # **The environment subject must be THIS stack's env** (``naming.env`` — ``dev`` / ``prod``),
        # not a fixed ``production``. A deploy workflow binds ``environment: ${{ inputs.environment }}``
        # so each environment can carry its own AWS account's role ARN, and **binding an environment
        # changes the OIDC token's ``sub``**: it becomes ``repo:<repo>:environment:<name>`` INSTEAD OF
        # ``repo:<repo>:ref:refs/heads/main``, not in addition to it. A role trusting only
        # ``…:environment:production`` therefore rejects the handshake with a bare
        # ``Not authorized to perform sts:AssumeRoleWithWebIdentity`` for a workflow bound to ``dev``
        # or ``prod`` — the ref subject never appears, so it cannot cover for the mismatch.
        #
        # The deploy role trusts ONLY the environment subject — deliberately not the ref subject too.
        # Keeping ``ref:refs/heads/main`` here would let any environment-FREE job on ``main`` with
        # ``id-token: write`` assume this role directly: no environment binding, so no Deployments
        # record, no branch policy, and — because each environment is a different AWS account — the
        # dev/prod isolation this stack exists to create is re-opened from either side. Nothing needs
        # it: the invoke workflow is the only environment-free workflow and it uses the *invoke* role,
        # which keeps its own ref subject below. A future unbound job that genuinely needs deploy
        # rights should be given its subject explicitly via ``-c deploy_subjects=``, so that widening
        # is a decision someone typed rather than a default nobody re-read.
        deploy_subjects = deploy_subjects or [f"repo:{github_repo}:environment:{naming.env}"]

        # Invoke trusts only ``repo:<repo>:ref:refs/heads/main``: a push or dispatch on the protected
        # branch. Never the ``pull_request`` subject, which any branch with an open PR can mint
        # without review (AppSec STRANDS-408); a PR-event run gets no invoke credentials.
        #
        # One shared invoke role can serve N repos: ``extra_invoke_repos`` adds each sibling repo's
        # ``:ref:refs/heads/main`` subject to THIS same role, so onboarding another repo is a
        # context knob, not a new role/stack. Default = just ``github_repo``.
        def _repo_invoke_subjects(repo: str) -> list[str]:
            return [f"repo:{repo}:ref:refs/heads/main"]

        invoke_repos = [github_repo, *(extra_invoke_repos or [])]
        invoke_subjects = invoke_subjects or [
            subject for repo in invoke_repos for subject in _repo_invoke_subjects(repo)
        ]
        # An override must not widen the trust back: the condition is ``StringLike``, so a wildcard
        # (``repo:<r>:*``) would readmit the ``pull_request`` subject. Fail synth instead.
        for subject in invoke_subjects:
            if any(c in subject for c in "*?") or not subject.endswith(":ref:refs/heads/main"):
                raise ValueError(
                    f"invoke_subjects entry {subject!r} must be an exact "
                    "repo:<repo>:ref:refs/heads/main subject (no wildcards, no pull_request)"
                )

        def principal(subjects: list[str]) -> iam.OpenIdConnectPrincipal:
            # StringEquals on the audience (exact), StringLike on the subject (supports `*`).
            return iam.OpenIdConnectPrincipal(
                provider,
                conditions={
                    "StringEquals": {f"{GITHUB_OIDC_HOSTNAME}:aud": GITHUB_OIDC_AUDIENCE},
                    "StringLike": {f"{GITHUB_OIDC_HOSTNAME}:sub": subjects},
                },
            )

        # --- 3. Deploy role (privileged, locked to protected refs) --------------------------------
        deploy_role = iam.Role(
            self,
            "DeployRole",
            role_name=naming.gha_deploy_role,
            assumed_by=principal(deploy_subjects),
            # Image builds + the multi-stack cdk deploy run well over 15 min; give
            # the assumed session room (configure-aws-credentials requests up to this).
            max_session_duration=Duration.hours(1),
            description=f"GitHub Actions OIDC deploy role for {github_repo} ({naming.env})",
        )

        if deploy_policy == "admin":
            # Escape hatch for easier devx in throwaway/dev accounts — full admin.
            deploy_role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            )
        else:
            # Curated breadth for `cdk deploy` of every stack here. Resources are `*` because CloudFormation/CDK provision arbitrary
            # named resources; the trust policy (protected refs only) is the real guardrail. This is
            # privileged by nature — that is exactly why it is split from the invoke role.
            deploy_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DeployServices",
                    actions=[
                        # CDK / CloudFormation provisioning engine + asset staging.
                        "cloudformation:*",
                        "s3:*",
                        "ssm:GetParameter",
                        "ssm:GetParameters",
                        "ecr:*",
                        # The agent runtime's VPC (subnets, NAT, gateway endpoints, security
                        # group) and the capacity provider's EC2 validation (AgentRuntimeStack).
                        "ec2:*",
                        "bedrock-agentcore:*",
                        # The control-plane API CreateAgentRuntime/UpdateAgentRuntime live under.
                        "bedrock:*",
                        # Resources the CDK stacks here create.
                        "dynamodb:*",
                        "lambda:*",
                        "apigateway:*",
                        "scheduler:*",
                        "cognito-idp:*",
                        "cognito-identity:*",
                        "cloudfront:*",
                        "secretsmanager:*",
                        "s3vectors:*",
                        "logs:*",
                        "xray:*",
                        "cloudwatch:*",
                        "events:*",
                        # The Monitoring + Audit stacks each create an SNS alert/findings topic
                        # (with an optional email subscription). Without this the scoped deploy
                        # role AccessDenies on sns:CreateTopic when those stacks are deployed by CI.
                        "sns:*",
                        "sts:GetCallerIdentity",
                    ],
                    resources=["*"],
                )
            )
            # IAM split out so the intent (CDK creates/passes execution, operator and instance roles)
            # is legible in review, rather than buried in a wildcard.
            deploy_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DeployIam",
                    actions=[
                        "iam:CreateRole",
                        "iam:DeleteRole",
                        "iam:GetRole",
                        "iam:TagRole",
                        "iam:UntagRole",
                        "iam:UpdateRole",
                        "iam:UpdateAssumeRolePolicy",
                        "iam:AttachRolePolicy",
                        "iam:DetachRolePolicy",
                        "iam:PutRolePolicy",
                        "iam:DeleteRolePolicy",
                        "iam:GetRolePolicy",
                        "iam:ListRolePolicies",
                        "iam:ListAttachedRolePolicies",
                        "iam:CreatePolicy",
                        "iam:DeletePolicy",
                        "iam:GetPolicy",
                        "iam:CreatePolicyVersion",
                        "iam:DeletePolicyVersion",
                        "iam:GetPolicyVersion",
                        "iam:ListPolicyVersions",
                        "iam:CreateServiceLinkedRole",
                        "iam:PassRole",
                        # The agent capacity provider's EC2 instance profile.
                        "iam:CreateInstanceProfile",
                        "iam:DeleteInstanceProfile",
                        "iam:GetInstanceProfile",
                        "iam:AddRoleToInstanceProfile",
                        "iam:RemoveRoleFromInstanceProfile",
                        "iam:TagInstanceProfile",
                        "iam:UntagInstanceProfile",
                    ],
                    resources=["*"],
                )
            )

        # --- 4. Invoke role (fire-and-forget: InvokeAgentRuntime on the agent runtime, nothing else) --
        invoke_role = iam.Role(
            self,
            "InvokeRole",
            role_name=naming.gha_invoke_role,
            assumed_by=principal(invoke_subjects),
            max_session_duration=Duration.hours(1),
            description=f"GitHub Actions OIDC invoke role for {github_repo} ({naming.env})",
        )

        # The one runtime when its ARN is known; otherwise the AGENT runtime names of this
        # deployment. Never ``runtime/*``: that would include the sandbox runtime, whose payload
        # carries the git credential. A runtime ARN is ``runtime/<runtime name>-<10-char suffix>``
        # and InvokeAgentRuntime authorizes both it and ``<runtime ARN>/runtime-endpoint/<name>``,
        # which the trailing ``*`` covers. ``<agent>-*`` is generation 1; ``<agent>_g?-*`` and
        # ``<agent>_g??-*`` are generations 2-99 (``Naming.agent_runtime_name``). The ``-`` right
        # after the generation keeps another deployment's names out (``<agent>_g*`` would match
        # ``strandly_dev_agent_gamma_sandbox_ec2`` of name ``strandly-dev-agent``, env ``gamma``).
        if runtime_arn:
            runtime_id = runtime_arn.rsplit(":runtime/", 1)[-1]
            if runtime_id.startswith(naming.sandbox_ec2()):
                raise ValueError("runtime_arn names the sandbox runtime; the invoke role never targets it")
            invoke_resources = [runtime_arn, f"{runtime_arn}/*"]
        else:
            agent_prefix = (
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/{naming.agent_runtime}"
            )
            invoke_resources = [f"{agent_prefix}-*", f"{agent_prefix}_g?-*", f"{agent_prefix}_g??-*"]
        invoke_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=invoke_resources,
            )
        )
        # No Memory read and no CloudFormation read: a caller that may invoke the agent must not
        # also read every session's transcript. Results come back through the agent's own replies.

        # --- 5. Outputs — paste these into the repo's GitHub secrets ------------------------------
        CfnOutput(
            self,
            "DeployRoleArn",
            value=deploy_role.role_arn,
            description="Set as the AWS_DEPLOY_ROLE_ARN GitHub secret (deploy workflows).",
        )
        CfnOutput(
            self,
            "InvokeRoleArn",
            value=invoke_role.role_arn,
            description="Set as the AWS_INVOKE_ROLE_ARN GitHub secret (invoke workflows).",
        )
        CfnOutput(
            self,
            "OidcProviderArn",
            value=provider.open_id_connect_provider_arn,
            description="The GitHub OIDC provider ARN (pass as -c oidc_provider_arn=… for other envs).",
        )
