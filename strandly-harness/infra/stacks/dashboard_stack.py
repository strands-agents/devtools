"""DashboardStack — the maintainer dashboard: Cognito + HTTP API + read Lambda + S3/CloudFront SPA.

Moved from ``dashboard/infra/stacks/dashboard_stack.py`` into the unified app. The run-ledger
DynamoDB table is owned by :class:`DataStack`; this stack references it **by its deterministic name**
(``from_table_attributes``), not by importing Data's live ``ITable``. That avoids an
``Fn::ImportValue`` + CloudFormation export — an export can't be modified while imported, which
deadlocks a re-deploy of Data. Deleting the dashboard still leaves the runtime's telemetry intact.
The SPA + API Lambda source still come from the repo's ``dashboard/`` directory (app code).

Wiring (no dependency cycle): the S3 + CloudFront site is created first so its domain is known;
Cognito's hosted-UI callback points at that domain; the HTTP API's Cognito JWT authorizer points at
the user pool. The SPA talks to the API by its own URL (CORS allows the CloudFront origin) rather
than proxying ``/api/*`` through CloudFront — that proxy would create a CloudFront→API→Cognito→
CloudFront cycle, so it's intentionally left as a later enhancement.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from .common import MENTION_LOG_GSI, RUN_LEDGER_GSI, Naming, dynamodb_table_arn

# infra/stacks/dashboard_stack.py -> repo root -> dashboard/
_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
_API_DIR = _DASHBOARD_DIR / "api"
_WEB_DIR = _DASHBOARD_DIR / "web"


class DashboardStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        cognito_domain_prefix: str | None = None,
        runtime_arn: str | None = None,
        memory_id: str | None = None,
        actor_id: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Reference the Data stack's run-ledger by its deterministic name (no cross-stack import →
        # no Fn::ImportValue/export → Data can be re-deployed freely). grant_index_permissions +
        # the "recent" GSI let the read Lambda's grant_read_data cover the GSI Query it runs.
        table = dynamodb.Table.from_table_attributes(
            self,
            "RunLedgerRef",
            table_arn=dynamodb_table_arn(
                naming.run_ledger_table, region=self.region, account=self.account
            ),
            global_indexes=[RUN_LEDGER_GSI],
            grant_index_permissions=True,
        )
        # The mention-log table (poller-written), read by the Mentions tab — same by-name pattern.
        mention_log = dynamodb.Table.from_table_attributes(
            self,
            "MentionLogRef",
            table_arn=dynamodb_table_arn(
                naming.mention_log_table, region=self.region, account=self.account
            ),
            global_indexes=[MENTION_LOG_GSI],
            grant_index_permissions=True,
        )

        site_bucket, distribution = self._static_site()
        site_url = f"https://{distribution.distribution_domain_name}"

        user_pool, user_pool_client, cognito_domain = self._cognito(
            naming, site_url, cognito_domain_prefix
        )
        cognito_hosted_ui = (
            f"https://{cognito_domain.domain_name}.auth.{self.region}.amazoncognito.com"
        )

        api = self._api(
            table, user_pool, user_pool_client, cognito_hosted_ui, site_url, naming, runtime_arn,
            memory_id, actor_id, mention_log=mention_log,
        )
        self._deploy_site(site_bucket, distribution, api, user_pool_client, cognito_hosted_ui)
        self._outputs(table, distribution, api, user_pool, user_pool_client, cognito_hosted_ui)

    # ---- static site (S3 + CloudFront) ----------------------------------------------

    def _static_site(self) -> tuple[s3.Bucket, cloudfront.Distribution]:
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            # SPA: serve index.html for client-side routes / the OAuth redirect path.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )
        return bucket, distribution

    def _deploy_site(
        self,
        bucket: s3.Bucket,
        distribution: cloudfront.Distribution,
        api: apigw.HttpApi,
        user_pool_client: cognito.UserPoolClient,
        cognito_hosted_ui: str,
    ) -> None:
        """Upload the SPA + a resolved config.json, then invalidate CloudFront."""
        config = {
            "apiBase": api.api_endpoint,
            "region": self.region,
            "clientId": user_pool_client.user_pool_client_id,
            "cognitoDomain": cognito_hosted_ui,
        }
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[
                s3deploy.Source.asset(str(_WEB_DIR)),
                s3deploy.Source.json_data("config.json", config),
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

    # ---- auth (Cognito) --------------------------------------------------------------

    def _cognito(
        self, naming: Naming, site_url: str, domain_prefix: str | None
    ) -> tuple[cognito.UserPool, cognito.UserPoolClient, cognito.UserPoolDomain]:
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,  # maintainers are invited, not self-registered
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        prefix = domain_prefix or f"{naming.hyphen}-dashboard-{self.account}"
        domain = user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
        )
        client = user_pool.add_client(
            "WebClient",
            generate_secret=False,  # public SPA client → OAuth2 Authorization-Code + PKCE
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[site_url, f"{site_url}/", "http://localhost:5173/"],
                logout_urls=[site_url, f"{site_url}/", "http://localhost:5173/"],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )
        return user_pool, client, domain

    # ---- read API (HTTP API + Lambda) ------------------------------------------------

    def _api(
        self,
        table: dynamodb.ITable,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        cognito_hosted_ui: str,
        site_url: str,
        naming: Naming,
        runtime_arn: str | None = None,
        memory_id: str | None = None,
        actor_id: str | None = None,
        mention_log: dynamodb.ITable | None = None,
    ) -> apigw.HttpApi:
        fn = lambda_.Function(
            self,
            "ReadApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(_API_DIR)),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "RUN_LEDGER_TABLE": table.table_name,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                "COGNITO_DOMAIN": cognito_hosted_ui,
                # AWS_REGION is provided by the Lambda runtime automatically (reserved).
            },
        )
        table.grant_read_data(fn)

        # Mentions tab: read-only on the poller-written mention log (GSI Query via grant_read_data).
        if mention_log is not None:
            fn.add_environment("MENTION_LOG_TABLE", mention_log.table_name)
            mention_log.grant_read_data(fn)

        # Overview health strip: the read Lambda may describe THIS deployment's alarms. The
        # MonitoringStack names them all "<naming.hyphen>-…", and the handler passes that prefix as
        # the DescribeAlarms `AlarmNamePrefix` so only this deployment's alarms come back. The IAM
        # resource, though, MUST be "*": cloudwatch:DescribeAlarms is a list-style action that does
        # not support resource-level permissions, so scoping it to an alarm ARN denies the call
        # outright (AccessDenied). Alarms are always created by Monitoring, so this is unconditional;
        # if Monitoring isn't deployed the call just returns an empty list and /api/health degrades.
        alarm_prefix = f"{naming.hyphen}-"
        fn.add_environment("ALARM_NAME_PREFIX", alarm_prefix)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:DescribeAlarms"],
                resources=["*"],
            )
        )

        # Chat is gated on a deployed runtime arn: when provided, the read Lambda may also invoke
        # the AgentCore runtime (fire-and-forget launch + poll) so the SPA's chat tab can continue a
        # session. Scoped to the one runtime (+ its sessions). Unset it and the chat routes 503.
        if runtime_arn:
            fn.add_environment("STRANDLY_RUNTIME_ARN", runtime_arn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[runtime_arn, f"{runtime_arn}/*"],
                )
            )

        # Transcripts are read from AgentCore Memory when a memory id is supplied: the read Lambda
        # gets AGENTCORE_MEMORY_ID (+ optional STRANDLY_ACTOR_ID so it addresses the same actor the
        # runtime wrote under) and a scoped bedrock-agentcore:ListEvents grant on that one memory
        # resource (+ its sessions). Unset it and /api/sessions/{id} falls back to the ledger-derived
        # transcript — exactly as before.
        if memory_id:
            fn.add_environment("AGENTCORE_MEMORY_ID", memory_id)
            if actor_id:
                fn.add_environment("STRANDLY_ACTOR_ID", actor_id)
            memory_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/{memory_id}"
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:ListEvents"],
                    resources=[memory_arn, f"{memory_arn}/*"],
                )
            )

        # Runtime logs in the Runs drawer: when a runtime arn is known, the read Lambda may filter
        # the runtime's CloudWatch log group (scoped to that ONE group + its streams) by session-id
        # prefix. AgentCore's group is /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT; derive
        # the id from the arn (…:runtime/<id>). Unset → the /runs/{id}/logs route returns empty.
        if runtime_arn:
            runtime_id = runtime_arn.split("/")[-1]
            log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
            fn.add_environment("RUNTIME_LOG_GROUP", log_group)
            log_group_arn = f"arn:aws:logs:{self.region}:{self.account}:log-group:{log_group}:*"
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    # DescribeLogStreams: the reader lists streams to substring-match the session
                    # marker (AgentCore date-prefixes the stream name); FilterLogEvents: read them.
                    actions=["logs:DescribeLogStreams", "logs:FilterLogEvents"],
                    resources=[log_group_arn],
                )
            )

        http_api = apigw.HttpApi(
            self,
            "ReadApi",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[site_url, "http://localhost:5173"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        integration = integrations.HttpLambdaIntegration("ReadApiIntegration", fn)
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer", user_pool, user_pool_clients=[user_pool_client]
        )

        # Authorized routes (maintainer JWT required).
        for path in (
            "/api/overview",
            "/api/health",
            "/api/runs",
            "/api/runs/{id}",
            "/api/runs/{id}/logs",
            "/api/sessions",
            "/api/sessions/{id}",
            "/api/mentions",
            "/api/chat",
        ):
            http_api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                integration=integration,
                authorizer=authorizer,
            )
        # Chat launch is a POST (still maintainer-authorized) — continue a session on the runtime.
        http_api.add_routes(
            path="/api/chat",
            methods=[apigw.HttpMethod.POST],
            integration=integration,
            authorizer=authorizer,
        )
        # Public route: the SPA needs the Cognito client id/domain *before* it can log in.
        http_api.add_routes(
            path="/api/config",
            methods=[apigw.HttpMethod.GET],
            integration=integration,
        )
        return http_api

    # ---- outputs ---------------------------------------------------------------------

    def _outputs(
        self,
        table: dynamodb.ITable,
        distribution: cloudfront.Distribution,
        api: apigw.HttpApi,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        cognito_hosted_ui: str,
    ) -> None:
        CfnOutput(self, "DashboardURL", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "ApiURL", value=api.api_endpoint)
        CfnOutput(self, "CognitoHostedUI", value=cognito_hosted_ui)
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(
            self,
            "RunLedgerTableName",
            value=table.table_name,
            description="Set as STRANDLY_RUN_LEDGER_TABLE on the deployed AgentCore runtime.",
        )
