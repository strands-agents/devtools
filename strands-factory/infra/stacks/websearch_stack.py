"""WebSearchStack: an AgentCore Gateway exposing the managed Web Search connector over MCP.

The gateway uses ``AWS_IAM`` inbound auth, so the agent signs requests with SigV4
(``STRANDLY_SEARCH_MCP_AUTH=sigv4``) instead of sending a bearer token.
"""

from __future__ import annotations

from collections.abc import Sequence

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from .common import Naming


class WebSearchStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        exec_role_names: Sequence[str] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        gateway = agentcore.Gateway(
            self,
            "Gateway",
            gateway_name=f"{naming.hyphen}-websearch",
            description="AWS-managed web search (Web Search Tool connector) for the Strandly agent.",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
        )

        # The web-search connector target — no L2 factory exists for this target type yet, so it's
        # built on the L1 resource directly. `configurations` (not `enabled`) is what the AWS docs'
        # actual working examples use to turn on the connector's one tool, "WebSearch"; an empty
        # `parameterValues` accepts service-managed defaults (see domain-filtering docs for the
        # optional `domainFilter.exclude` override).
        agentcore.CfnGatewayTarget(
            self,
            "WebSearchTarget",
            gateway_identifier=gateway.gateway_id,
            name="web-search",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    connector=agentcore.CfnGatewayTarget.ConnectorTargetConfigurationProperty(
                        source=agentcore.CfnGatewayTarget.ConnectorSourceProperty(
                            connector_id="web-search"
                        ),
                        configurations=[
                            agentcore.CfnGatewayTarget.ConnectorConfigurationProperty(
                                name="WebSearch", parameter_values={}
                            )
                        ],
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )

        # The Gateway's OWN service role (not the caller's) needs its own permissions to reach the
        # web-search backend — the L2's auto-generated role only carries the AgentCore trust policy,
        # no permissions. Per AWS's docs: InvokeGateway (self-invoke, required regardless of target
        # type) + InvokeWebSearch scoped to the service-owned tool ARN (checked per-request).
        gateway.role.attach_inline_policy(
            iam.Policy(
                self,
                "GatewayServiceRolePolicy",
                statements=[
                    iam.PolicyStatement(
                        sid="InvokeGateway",
                        actions=["bedrock-agentcore:InvokeGateway"],
                        resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
                    ),
                    iam.PolicyStatement(
                        sid="InvokeWebSearch",
                        actions=["bedrock-agentcore:InvokeWebSearch"],
                        resources=[f"arn:aws:bedrock-agentcore:{self.region}:aws:tool/web-search.v1"],
                    ),
                ],
            )
        )

        # Grant the agent runtimes' execution roles SigV4 invoke access — AWS_IAM auth means
        # "authorized to call this Gateway" IS the credential; no bearer token to mint/rotate.
        # Every role named (a deploy names exactly the AgentRuntime stack's role), sorted so the
        # template does not depend on the order. Construct path ExecRole/Policy keeps the logical id (and
        # policy name) the single-role `gateway.grant_invoke` produced, so a deployed stack updates
        # the policy in place instead of replacing it.
        roles = sorted(set(exec_role_names))
        if roles:
            iam.Policy(
                Construct(self, "ExecRole"),
                "Policy",
                statements=[
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:InvokeGateway"],
                        resources=[gateway.gateway_arn],
                    )
                ],
                roles=[
                    iam.Role.from_role_name(self, f"ImportedExecRole{i}", name)
                    for i, name in enumerate(roles)
                ],
            )

        CfnOutput(self, "GatewayUrl", value=gateway.gateway_url)
        CfnOutput(self, "GatewayArn", value=gateway.gateway_arn)
