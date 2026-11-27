"""DashboardStack: the Maintainer Portal, its Cognito pool, the read API and the schedules API.

The portal (``dashboard/web-next``) is served from a private bucket behind CloudFront; Cognito and
the portal share one stack because the client's callback URLs name the distribution while the
portal's ``config.json`` names the client. The read API (``dashboard/api/handler.py``) and the
schedules API run as separate Lambdas behind one HTTP API with a Cognito authorizer. With a runtime,
the GitHub Actions ingress (``dashboard/api/actions.py``) is a third Lambda on the same API behind a
JWT authorizer for the GitHub Actions OIDC issuer.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import AssetHashType, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from .common import (
    MEMORY_READER_ACTIONS,
    PEOPLE_COGNITO_GSI,
    PEOPLE_GITHUB_GSI,
    RUN_LEDGER_GSI,
    SESSION_TITLE_KEY_PREFIX,
    WORK_PRODUCT_SESSION_GSI,
    Naming,
    dynamodb_table_arn,
    schedules_cap_environment,
    session_store_statements,
)
from .lambda_sdk import TZDATA_REQUIREMENT, sdk_layer_dir
from .runtime_common import bounded_log_group

# infra/stacks/dashboard_stack.py -> repo root -> dashboard/
_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"
_API_DIR = _DASHBOARD_DIR / "api"
# infra/stacks/dashboard_stack.py -> repo root -> src/ (the strandly source package)
_SRC_DIR = Path(__file__).resolve().parents[2] / "harness" / "src"

# The Maintainer Portal (web-next) is a Vite app — its bytes only exist under dist/ AFTER
# `npm ci && npm run build` (run that before a deploy synth). dist/ is
# absent at test/synth time, so its upload is guarded on the directory existing (see
# _deploy_portal): synth still produces the bucket + distribution + Cognito callbacks +
# config.json, it just skips the (missing) HTML source.
_WEB_NEXT_DIR = _DASHBOARD_DIR / "web-next"
_WEB_NEXT_DIST = _WEB_NEXT_DIR / "dist"

# The GitHub Actions OIDC issuer; mirrors dashboard/api/actions.py ACTIONS_ISSUER.
ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"


@dataclass(frozen=True)
class ActionsIngress:
    """Which GitHub Actions workflows may call the agent (the ``actions_*`` context keys)."""

    audience: str = "strandly"
    repositories: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    default_branch: str = "main"


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
        schedules_enabled: bool = True,
        schedules_max_active: int | None = None,
        portal_identity: dict[str, object] | None = None,
        actions: ActionsIngress | None = None,
        dev_callbacks: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._schedules_enabled = schedules_enabled
        self._schedules_cap = schedules_cap_environment(schedules_max_active)
        self._actions = actions
        # The Vite dev server origins (web-next uses 5174; 5173 is Vite's default). They are Cognito
        # callback/logout URLs and CORS origins only in a development deployment: any local process
        # on those ports could otherwise receive a production sign-in redirect.
        self._dev_origins: tuple[str, ...] = (
            ("http://localhost:5173", "http://localhost:5174") if dev_callbacks else ()
        )

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
        # The work-product table (runtime-written), read by the portal's per-session card — same
        # by-name pattern, with the by_session GSI so grant_read_data covers its Query.
        work_products = dynamodb.Table.from_table_attributes(
            self,
            "WorkProductRef",
            table_arn=dynamodb_table_arn(
                naming.work_product_table, region=self.region, account=self.account
            ),
            global_indexes=[WORK_PRODUCT_SESSION_GSI],
            grant_index_permissions=True,
        )

        cognito_prefix = cognito_domain_prefix or f"{naming.hyphen}-dashboard-{self.account}"
        portal_bucket, portal_distribution = self._static_site("Portal", cognito_prefix)
        portal_url = f"https://{portal_distribution.distribution_domain_name}"

        user_pool, user_pool_client, cognito_domain = self._cognito(portal_url, cognito_prefix)
        cognito_hosted_ui = (
            f"https://{cognito_domain.domain_name}.auth.{self.region}.amazoncognito.com"
        )

        api = self._api(
            table, user_pool, user_pool_client, cognito_hosted_ui, portal_url, naming, runtime_arn,
            memory_id, work_products=work_products,
        )
        # The portal's runtime config.json: dataSource=live, the API + Cognito, and its OWN URL as
        # the PKCE redirectUri (see web-next's src/api/config.ts for the fields it reads).
        portal_config = {
            "dataSource": "live",
            "portalIdentity": portal_identity or {},
            "apiBase": api.api_endpoint,
            "region": self.region,
            "cognitoDomain": cognito_hosted_ui,
            "cognitoClientId": user_pool_client.user_pool_client_id,
            "redirectUri": portal_url,
        }
        self._deploy_portal(portal_bucket, portal_distribution, portal_config)
        self._outputs(
            table, api, user_pool, user_pool_client, cognito_hosted_ui,
            portal_distribution, portal_bucket,
        )

    # ---- static site (S3 + CloudFront) ----------------------------------------------

    def _deploy_log_group(self) -> logs.LogGroup:
        """One bounded log group for the (singleton) BucketDeployment handler."""
        return bounded_log_group(self, "DeployLogGroup")

    def _static_site(
        self, id_prefix: str, cognito_prefix: str
    ) -> tuple[s3.Bucket, cloudfront.Distribution]:
        """A private S3 bucket + CloudFront distribution (OAC, SPA 403/404 → /index.html).

        ``id_prefix`` is ``"Portal"``; the resulting logical ids (PortalBucket/PortalDistribution)
        must not change, or CloudFormation would replace the live resources.
        """
        bucket = s3.Bucket(
            self,
            f"{id_prefix}Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            f"{id_prefix}Distribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=self._security_headers(id_prefix, cognito_prefix),
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

    def _security_headers(
        self, id_prefix: str, cognito_prefix: str
    ) -> cloudfront.ResponseHeadersPolicy:
        """Browser hardening headers for the portal: CSP, HSTS, no framing, no MIME sniffing.

        ``connect-src`` names every origin the SPA fetches: its own ``config.json``, the dashboard
        API, the Hosted UI token endpoint and the GitHub API. The API is matched by the regional
        execute-api wildcard because naming its id here would be a dependency cycle (the API's CORS
        list names this distribution). ``style-src 'unsafe-inline'`` is for Mantine's runtime
        ``<style>`` tags and the inline paint style in ``index.html``; scripts stay ``'self'``.
        """
        hosted_ui = f"https://{cognito_prefix}.auth.{self.region}.amazoncognito.com"
        csp = "; ".join(
            (
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: blob: https://github.com https://*.githubusercontent.com",
                "font-src 'self' data:",
                "connect-src 'self' https://api.github.com "
                f"https://*.execute-api.{self.region}.amazonaws.com {hosted_ui}",
                "worker-src 'self'",
                "manifest-src 'self'",
                "object-src 'none'",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            )
        )
        return cloudfront.ResponseHeadersPolicy(
            self,
            f"{id_prefix}SecurityHeaders",
            comment="Strandly portal security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365 * 2),
                    include_subdomains=True,
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )

    def _deploy_portal(
        self,
        bucket: s3.Bucket,
        distribution: cloudfront.Distribution,
        portal_config: dict[str, str],
    ) -> None:
        """Upload the built web-next bundle + its resolved config.json, then invalidate CloudFront.

        The HTML bytes live under ``web-next/dist`` only after a Vite build (``npm ci && npm run build``
        before a deploy synth). That directory is absent at test/synth time, so
        its source is added ONLY when it exists — synth (and the infra tests) still get the bucket,
        the distribution, the Cognito callbacks and config.json; the deploy path, having built dist,
        also ships the bundle. config.json (json_data) is always present, so BucketDeployment always
        has at least one source.
        """
        sources = [s3deploy.Source.json_data("config.json", portal_config)]
        if _WEB_NEXT_DIST.is_dir():
            sources.insert(0, s3deploy.Source.asset(str(_WEB_NEXT_DIST)))
        s3deploy.BucketDeployment(
            self,
            "DeployPortal",
            sources=sources,
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            log_group=self._deploy_log_group(),
        )

    # ---- auth (Cognito) --------------------------------------------------------------

    def _cognito(
        self, portal_url: str, prefix: str
    ) -> tuple[cognito.UserPool, cognito.UserPoolClient, cognito.UserPoolDomain]:
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,  # maintainers are invited, not self-registered
            sign_in_aliases=cognito.SignInAliases(email=True),
            # Every pool member can read every session, so one leaked password must not be enough.
            # Managed login enrolls the TOTP app on a user's first sign-in after this turns on.
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=True),
            # RETAIN: a stack teardown or replacement must never wipe the maintainer accounts.
            removal_policy=RemovalPolicy.RETAIN,
        )
        domain = user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
        )
        dev_redirects = [f"{origin}/" for origin in self._dev_origins]
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
                # The portal's PKCE redirect (redirectUri = its own URL, written into its
                # config.json) fails with redirect_mismatch unless portal_url is listed here.
                callback_urls=[
                    portal_url,
                    f"{portal_url}/",
                    *dev_redirects,
                    # `strandly login`'s loopback redirect (the CLI PKCE flow, see
                    # cli/strandly-mcp/src/strandly_mcp/auth.py LOGIN_REDIRECTS).
                    "http://localhost:8765/callback",
                ],
                logout_urls=[portal_url, f"{portal_url}/", *dev_redirects],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            # Bounds how long a stolen refresh token (portal localStorage or the CLI's auth.json)
            # stays usable; a CLI or portal user signs in again after 30 days.
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )
        return user_pool, client, domain

    # ---- read API (HTTP API + Lambda) ------------------------------------------------

    def _sdk_layer(
        self,
        construct_id: str = "AwsSdkLayer",
        *,
        extra: str = "",
        description: str = "boto3/botocore pinned from uv.lock (newer than the Lambda runtime SDK)",
    ) -> lambda_.LayerVersion:
        """boto3 + botocore at the ``uv.lock`` pins (+ ``extra`` pinned wheels), as a layer
        (``/opt/python`` shadows the runtime's SDK). Asset hash = digest of the pins, so it is
        deterministic and rolls exactly when they do. Built (pure-Python wheels, no Docker) and
        cached at synth."""
        layer_dir, key = sdk_layer_dir(extra=extra)
        return lambda_.LayerVersion(
            self,
            construct_id,
            code=lambda_.Code.from_asset(
                str(layer_dir),
                asset_hash=key,
                asset_hash_type=AssetHashType.CUSTOM,
                # Importing from the cached build dir (a local check) writes bytecode; never ship it.
                exclude=["**/__pycache__", "**/*.pyc"],
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description=description,
        )

    def _api(
        self,
        table: dynamodb.ITable,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        cognito_hosted_ui: str,
        portal_url: str,
        naming: Naming,
        runtime_arn: str | None = None,
        memory_id: str | None = None,
        work_products: dynamodb.ITable | None = None,
    ) -> apigw.HttpApi:
        sdk_layer = self._sdk_layer()
        fn = lambda_.Function(
            self,
            "ReadApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            # Bytecode excluded: a local test run's __pycache__ would otherwise change the asset hash.
            code=lambda_.Code.from_asset(str(dashboard_api_dir(api_dir=_API_DIR))),
            # The uv.lock-pinned boto3/botocore, NOT the runtime's bundled SDK: that one predates
            # the bedrock-agentcore PayloadType ``json`` member, so ListEvents silently dropped
            # every tool-call/tool-result transcript message (see stacks/lambda_sdk.py).
            layers=[sdk_layer],
            # 29s, just under the HTTP API's 30s integration cap: a chat launch can wait ~25s.
            timeout=Duration.seconds(29),
            memory_size=512,
            log_group=bounded_log_group(self, "ReadApiLogGroup"),
            environment={
                "RUN_LEDGER_TABLE": table.table_name,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                "COGNITO_DOMAIN": cognito_hosted_ui,
                # AWS_REGION is provided by the Lambda runtime automatically (reserved).
            },
        )
        table.grant_read_data(fn)
        # Session titles (PATCH /api/sessions/{id}): PutItem on the title rows only, never a run row.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[table.table_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": [f"{SESSION_TITLE_KEY_PREFIX}*"]
                    }
                },
            )
        )

        # Work products: read-only (the by_session Query). The handler gates its route on this env.
        if work_products is not None:
            fn.add_environment("STRANDLY_WORK_PRODUCT_TABLE", work_products.table_name)
            work_products.grant_read_data(fn)

        # The people directory (DataStack-owned, by name): chat launches map the verified Cognito
        # sub to its bound person (by_cognito), and a direct invoke's X-Strandly-Person login to
        # its person (by_github). Query on those indexes only, never the base table or a write;
        # the agent runtime still has no grant on it.
        fn.add_environment("STRANDLY_PEOPLE_TABLE", naming.people_table)
        people_arn = dynamodb_table_arn(naming.people_table, region=self.region, account=self.account)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[
                    f"{people_arn}/index/{PEOPLE_GITHUB_GSI}",
                    f"{people_arn}/index/{PEOPLE_COGNITO_GSI}",
                ],
            )
        )

        # Session transcripts: the read Lambda reads the session store (each session's actor marker
        # and latest snapshot) — read-only object + prefix-scoped list grants
        # and decrypt on the session CMK (shared helper with the runtime's grant, write=False).
        # By deterministic name, same no-cross-stack-import rationale as the tables above.
        session_bucket = naming.session_bucket(self.account)
        fn.add_environment("STRANDLY_SESSION_BUCKET", session_bucket)
        for statement in session_store_statements(
            session_bucket, naming, self.region, self.account, write=False
        ):
            fn.add_to_role_policy(statement)

        # Chat is gated on a deployed runtime arn: when provided, the read Lambda may also invoke
        # the AgentCore runtime (fire-and-forget launch + poll) so the portal's chat can continue a
        # session. Scoped to the one runtime (+ its sessions). Unset it and the chat routes 503.
        if runtime_arn:
            fn.add_environment("STRANDLY_RUNTIME_ARN", runtime_arn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[runtime_arn, f"{runtime_arn}/*"],
                )
            )

        # Transcripts: with a memory id the read Lambda gets AGENTCORE_MEMORY_ID and may read events
        # (ListEvents/GetEvent, nothing else) on that one Memory — the transcript events of every
        # session (team and DM actors). The session's actor comes from the session store (read
        # above). Unset it and /api/sessions/{id} shows the latest snapshot, else the ledger-derived
        # transcript.
        if memory_id:
            fn.add_environment("AGENTCORE_MEMORY_ID", memory_id)
            memory_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/{memory_id}"
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=list(MEMORY_READER_ACTIONS),
                    resources=[memory_arn, f"{memory_arn}/*"],
                )
            )

        # The portal is served from its own CloudFront origin, so browser calls to this API are
        # cross-origin — its URL must be in the CORS allow-list or every fetch fails the preflight.
        allow_origins = [*self._dev_origins, portal_url]
        http_api = apigw.HttpApi(
            self,
            "ReadApi",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=allow_origins,
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.PUT,
                    apigw.CorsHttpMethod.PATCH,
                    apigw.CorsHttpMethod.DELETE,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                # x-strandly-person: the portal's advisory "who are you" login (handler.py).
                allow_headers=["authorization", "content-type", "x-strandly-person"],
                max_age=Duration.hours(1),
            ),
        )
        integration = integrations.HttpLambdaIntegration("ReadApiIntegration", fn)
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer", user_pool, user_pool_clients=[user_pool_client]
        )

        # Authorized routes (maintainer JWT required): the portal's and strandly-mcp's surface.
        for path in ("/api/sessions", "/api/sessions/{id}", "/api/workproducts", "/api/chat"):
            http_api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                integration=integration,
                authorizer=authorizer,
            )
        # Session title rename.
        http_api.add_routes(
            path="/api/sessions/{id}",
            methods=[apigw.HttpMethod.PATCH],
            integration=integration,
            authorizer=authorizer,
        )
        # Chat launch / cancel.
        http_api.add_routes(
            path="/api/chat",
            methods=[apigw.HttpMethod.POST],
            integration=integration,
            authorizer=authorizer,
        )
        # Scheduled jobs live in the Scheduler stack, which exists only with a runtime.
        if runtime_arn:
            self._schedules_api(http_api, authorizer, naming, runtime_arn, table, user_pool)
        if runtime_arn and self._actions is not None:
            self._actions_api(http_api, runtime_arn, sdk_layer, self._actions)
        return http_api

    def _actions_api(
        self,
        http_api: apigw.HttpApi,
        runtime_arn: str,
        sdk_layer: lambda_.ILayerVersion,
        actions: ActionsIngress,
    ) -> None:
        """``/api/actions/invoke``: GitHub Actions workflows launch and poll runs with an OIDC token.

        Its own Lambda, so a workflow token reaches code that may only invoke the one runtime: no
        ledger, session store, Memory or Cognito grant. API Gateway verifies the token's signature,
        expiry, issuer and audience; the Lambda applies the repository/branch/event/workflow rules.
        """
        fn = lambda_.Function(
            self,
            "ActionsApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="actions.lambda_handler",
            code=lambda_.Code.from_asset(str(dashboard_api_dir(api_dir=_API_DIR))),
            layers=[sdk_layer],
            # Same 29 s as the read API: a launch can wait ~25 s on a cold session.
            timeout=Duration.seconds(29),
            memory_size=256,
            log_group=bounded_log_group(self, "ActionsApiLogGroup"),
            environment={
                "STRANDLY_RUNTIME_ARN": runtime_arn,
                "STRANDLY_ACTIONS_AUDIENCE": actions.audience,
                "STRANDLY_ACTIONS_REPOSITORIES": ",".join(actions.repositories),
                "STRANDLY_ACTIONS_WORKFLOWS": ",".join(actions.workflows),
                "STRANDLY_ACTIONS_DEFAULT_BRANCH": actions.default_branch,
            },
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        authorizer = authorizers.HttpJwtAuthorizer(
            "GitHubActionsAuthorizer",
            ACTIONS_ISSUER,
            jwt_audience=[actions.audience],
            identity_source=["$request.header.Authorization"],
        )
        http_api.add_routes(
            path="/api/actions/invoke",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("ActionsApiIntegration", fn),
            authorizer=authorizer,
        )

    def _schedules_api(
        self,
        http_api: apigw.HttpApi,
        authorizer: authorizers.HttpUserPoolAuthorizer,
        naming: Naming,
        runtime_arn: str,
        ledger: dynamodb.ITable,
        user_pool: cognito.UserPool,
    ) -> lambda_.Function:
        """The portal's ``/api/schedules*`` routes (``ops/lambdas/schedules_api``).

        Its OWN function and role, not routes on ReadApiFn: ReadApiFn is also invoked directly,
        around the Cognito authorizer, so writes there would reach every principal that may invoke
        it. Here the reach is scheduled jobs only. The resource policy admits only API Gateway; a
        same-account principal granted invoke by IAM acts as the person its ``X-Strandly-Person``
        names, else the shared direct-invoke actor (see the handler). It writes through the harness ``ScheduleService``:
        read/write on the schedules table, create/update/delete/get schedules in the jobs group,
        pass only the jobs scheduler role (to Scheduler), invoke the one runtime (run now), read the
        run ledger (past runs). The JWT subject and issuer are the stable actor identity.

        Packaged from ``src/`` like people-admin (``ops/`` is stdlib+boto3), with a layer carrying
        the pinned SDK and ``tzdata`` (job timezones resolve with ``zoneinfo``).
        """
        layer = self._sdk_layer(
            "SchedulesSdkLayer",
            extra=TZDATA_REQUIREMENT,
            description="boto3/botocore pinned from uv.lock + tzdata (scheduled-job timezones)",
        )
        jobs_role_arn = f"arn:{self.partition}:iam::{self.account}:role/{naming.jobs_scheduler_role}"
        invoker_arn = (
            f"arn:{self.partition}:lambda:{self.region}:{self.account}"
            f":function:{naming.scheduler_function}"
        )
        fn = lambda_.Function(
            self,
            "SchedulesApiFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly.ops.lambdas.schedules_api.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(_SRC_DIR), exclude=["**/__pycache__", "**/*.pyc"]),
            layers=[layer],
            # Just under the HTTP API's 30 s cap; run now bounds its launch to fit (see handler).
            timeout=Duration.seconds(29),
            memory_size=512,
            log_group=bounded_log_group(self, "SchedulesApiLogGroup"),
            environment={
                "STRANDLY_SCHEDULES_TABLE": naming.schedules_table,
                "STRANDLY_SCHEDULES_ENABLED": "true" if self._schedules_enabled else "false",
                "STRANDLY_SCHEDULES_GROUP": naming.jobs_schedule_group,
                "STRANDLY_SCHEDULES_TARGET_ARN": invoker_arn,
                "STRANDLY_SCHEDULES_ROLE_ARN": jobs_role_arn,
                "STRANDLY_RUNTIME_ARN": runtime_arn,
                "STRANDLY_RUN_LEDGER_TABLE": ledger.table_name,
                "COGNITO_ISSUER": f"https://cognito-idp.{self.region}.{self.url_suffix}/{user_pool.user_pool_id}",
                **self._schedules_cap,
            },
        )
        schedules = dynamodb.Table.from_table_attributes(
            self,
            "SchedulesTableRef",
            table_arn=dynamodb_table_arn(
                naming.schedules_table, region=self.region, account=self.account
            ),
        )
        schedules.grant_read_write_data(fn)
        ledger.grant_read_data(fn)
        # A direct invoke acts as the person its X-Strandly-Person login names: Query on the
        # people directory's by_github index only.
        fn.add_environment("STRANDLY_PEOPLE_TABLE", naming.people_table)
        people_arn = dynamodb_table_arn(naming.people_table, region=self.region, account=self.account)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PeopleByGithub",
                actions=["dynamodb:Query"],
                resources=[f"{people_arn}/index/{PEOPLE_GITHUB_GSI}"],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="JobSchedules",
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                    "scheduler:GetSchedule",
                ],
                resources=[
                    f"arn:{self.partition}:scheduler:{self.region}:{self.account}"
                    f":schedule/{naming.jobs_schedule_group}/*"
                ],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassJobsSchedulerRole",
                actions=["iam:PassRole"],
                resources=[jobs_role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="RunNow",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        integration = integrations.HttpLambdaIntegration("SchedulesApiIntegration", fn)
        routes: tuple[tuple[str, tuple[apigw.HttpMethod, ...]], ...] = (
            ("/api/schedules", (apigw.HttpMethod.GET, apigw.HttpMethod.POST)),
            ("/api/schedules/preview", (apigw.HttpMethod.POST,)),
            (
                "/api/schedules/{id}",
                (apigw.HttpMethod.GET, apigw.HttpMethod.PUT, apigw.HttpMethod.DELETE),
            ),
            ("/api/schedules/{id}/runs", (apigw.HttpMethod.GET,)),
            *(
                (f"/api/schedules/{{id}}/{action}", (apigw.HttpMethod.POST,))
                for action in ("pause", "resume", "run", "revert")
            ),
        )
        for path, methods in routes:
            http_api.add_routes(
                path=path, methods=list(methods), integration=integration, authorizer=authorizer,
                authorization_scopes=["openid"],
            )
        return fn

    # ---- outputs ---------------------------------------------------------------------

    def _outputs(
        self,
        table: dynamodb.ITable,
        api: apigw.HttpApi,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        cognito_hosted_ui: str,
        portal_distribution: cloudfront.Distribution,
        portal_bucket: s3.Bucket,
    ) -> None:
        CfnOutput(
            self,
            "PortalURL",
            value=f"https://{portal_distribution.distribution_domain_name}",
            description="The Maintainer Portal (web-next) CloudFront URL.",
        )
        CfnOutput(
            self,
            "PortalBucketName",
            value=portal_bucket.bucket_name,
            description="The Maintainer Portal's S3 bucket.",
        )
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


_ROOT = Path(__file__).resolve().parents[2]


# The other stdlib-only harness modules the handler imports: the people directory (ops/people.py)
# and the constants it reads.
_SHARED_MODULES = ("strandly/core/constants.py", "strandly/ops/__init__.py", "strandly/ops/people.py")


def dashboard_api_dir(
    api_dir: Path = _ROOT / "dashboard/api",
    shared: Path = _ROOT / "harness/src/strandly/core/session_ids.py",
    build_root: Path = _ROOT / "infra/build/dashboard-api",
    modules: tuple[str, ...] = _SHARED_MODULES,
    harness_src: Path = _ROOT / "harness/src",
) -> Path:
    files = {
        str(path.relative_to(api_dir)): path.read_bytes()
        for path in sorted(api_dir.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    # Only these stdlib modules of the package ship; the empty package files make them importable.
    files["strandly/__init__.py"] = b""
    files["strandly/core/__init__.py"] = b""
    files["strandly/core/session_ids.py"] = shared.read_bytes()
    for name in modules:
        files[name] = (harness_src / name).read_bytes()
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    destination = build_root / digest.hexdigest()[:20]
    if destination.is_dir():
        return destination
    build_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".building-", dir=build_root))
    try:
        for name, content in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        try:
            staging.rename(destination)
        except OSError:
            if not destination.is_dir():  # Another synth may have finished the same asset.
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination
