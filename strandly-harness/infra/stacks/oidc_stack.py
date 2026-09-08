"""OidcStack — GitHub Actions OIDC federation for deploying *and* invoking the agent.

This replaces the imperative ``setup-aws-oidc.sh`` (create the OIDC provider + one all-powerful role
with the AWS CLI) with a declarative, reviewable CDK stack. It stands up the GitHub OIDC identity
provider and **two purpose-scoped roles**, and emits their ARNs as stack outputs you paste into the
repo's GitHub secrets — no static AWS keys anywhere.

Two roles, not one, because deploying and invoking the agent are different blast radii:

- **Deploy role** (``<name>-<env>-gha-deploy``) — privileged. It runs ``strandly deploy`` (the
  bedrock-agentcore toolkit's cloud build → ECR → CreateAgentRuntime) and ``cdk deploy`` of every
  stack here, so it needs CloudFormation/IAM/ECR/CodeBuild/Bedrock breadth. Its trust is therefore
  **locked to the repo's protected refs** (``main`` + the ``production`` environment by default) so
  only reviewed, merged code can wield it.
- **Invoke role** (``<name>-<env>-gha-invoke``) — minimal. It can only
  ``InvokeAgentRuntime`` (and read AgentCore Memory back for ``strandly poll``), scoped to the one
  runtime when its ARN is known. A workflow that merely *talks to* the agent never gets the keys to
  *redeploy* it.

Easier-devx knobs (the issue asked to "consider alternatives for easier devx"):

- The OIDC provider is an **account-global singleton** — a second env (or a sibling repo) trying to
  create it again fails with ``EntityAlreadyExists``. Pass ``-c oidc_provider_arn=<arn>`` to *import*
  the existing one instead of creating it; the roles attach to it either way.
- Everything else is a context knob with a sensible default: ``-c github_repo=`` (defaults to
  ``strands-agents/devtools``), ``-c deploy_subjects=`` / ``-c invoke_subjects=`` (comma-separated
  ``sub`` claim patterns), ``-c runtime_arn=`` / ``-c memory_id=`` to scope the invoke role,
  ``-c deploy_policy=admin`` to swap the curated deploy policy for ``AdministratorAccess``.

This stack is **always synthesized** (it has working defaults), but it owns no app state, so deploy
it independently: ``cdk deploy 'Strandly-Oidc-<env>'``.
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
        oidc_provider_arn: str | None = None,
        runtime_arn: str | None = None,
        memory_id: str | None = None,
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
        # Deploy is privileged → default to protected refs only (merged main + the `production`
        # GitHub environment). Invoke is lower-risk but still defaults to main; widen explicitly.
        deploy_subjects = deploy_subjects or [
            f"repo:{github_repo}:ref:refs/heads/main",
            f"repo:{github_repo}:environment:production",
        ]
        invoke_subjects = invoke_subjects or [f"repo:{github_repo}:ref:refs/heads/main"]

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
            # Cloud build + CreateAgentRuntime + multi-stack cdk deploy run well over 15 min; give
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
            # Curated breadth for `cdk deploy` of every stack here + the agentcore toolkit's
            # cloud-build/deploy. Resources are `*` because CloudFormation/CDK provision arbitrary
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
                        # agentcore starter toolkit: cloud build → image → runtime.
                        "codebuild:*",
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
                        "sts:GetCallerIdentity",
                    ],
                    resources=["*"],
                )
            )
            # IAM split out so the intent (the toolkit + CDK create/pass execution & service roles)
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
                    ],
                    resources=["*"],
                )
            )

        # --- 4. Invoke role (minimal: talk to the deployed runtime, read the result) -------------
        invoke_role = iam.Role(
            self,
            "InvokeRole",
            role_name=naming.gha_invoke_role,
            assumed_by=principal(invoke_subjects),
            max_session_duration=Duration.hours(1),
            description=f"GitHub Actions OIDC invoke role for {github_repo} ({naming.env})",
        )

        # Scope InvokeAgentRuntime to the one runtime (+ its endpoints/sessions) when known; else a
        # region/account-wide runtime wildcard. Mirrors the dashboard/ingress invoke scoping.
        if runtime_arn:
            invoke_resources = [runtime_arn, f"{runtime_arn}/*"]
        else:
            invoke_resources = [f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"]
        invoke_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeRuntime",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=invoke_resources,
            )
        )

        # `strandly poll` reads the run's result back from AgentCore Memory (ListEvents/GetEvent).
        # Scope to the one memory when known; else a memory wildcard. Read-only — never CreateEvent.
        if memory_id:
            memory_resources = [
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/{memory_id}",
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/{memory_id}/*",
            ]
        else:
            memory_resources = [f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"]
        invoke_role.add_to_policy(
            iam.PolicyStatement(
                sid="PollMemory",
                actions=[
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListSessions",
                ],
                resources=memory_resources,
            )
        )

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
