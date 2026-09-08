"""SchedulerStack — time-triggered self-invocations: one Lambda, one EventBridge schedule per job.

The agent's scheduled work (a daily activity review, …) is defined in the harness at
``src/strandly_harness/ops/lambdas/scheduled/jobs.py`` (the single source of truth — behavior lives in code).
This stack creates **one** generic invoker Lambda and **one EventBridge schedule per job**; each
schedule fires the Lambda with ``{"job": "<name>"}`` as its input, and the Lambda looks the job up,
builds its prompt, and dispatches the deployed runtime fire-and-forget.

The CDK runs in a venv that can't import the harness (it would pull the Strands SDK), so this stack
reads the job list out of ``jobs.py`` **statically** — it pulls only ``name`` / ``schedule`` /
``enabled`` (the fields CloudFormation needs) via the dependency-free helper below. The prompt and
skill never leave the harness; the Lambda (which runs *in* the harness package) reads them at fire
time. So adding a job = a new entry in ``jobs.py`` + a redeploy; no stack change.

IAM mirrors the ingress poller: the Lambda may only ``InvokeAgentRuntime`` on the one runtime, and
(when given) read the one config secret. Each schedule has a scheduler role that may invoke only
this Lambda, with an ``aws:SourceAccount`` confused-deputy guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from .common import Naming

# infra/stacks/scheduler_stack.py -> repo root -> the harness job registry (moved under ops/lambdas/
# in the core/serve/ops refactor — this static-read path must track it or `cdk synth` fails).
_JOBS_FILE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "strandly_harness"
    / "ops"
    / "lambdas"
    / "scheduled"
    / "jobs.py"
)
_DEFAULT_ASSET = Path(__file__).resolve().parents[1] / "build" / "poller"


def load_jobs(jobs_file: Path = _JOBS_FILE) -> list[dict[str, object]]:
    """Statically read ``JOBS`` from jobs.py — name/schedule/enabled only, no harness import.

    Parses the file with ``ast`` and pulls each ``ScheduledJob(...)`` call's fields. It only
    ``literal_eval``s the three fields CloudFormation needs (``name``/``schedule``/``enabled``) —
    the ``prompt``/``skill``/``session_prefix`` args are **never evaluated**, so a prompt written as
    an f-string, a concatenation, or ``.format()`` can't break ``cdk synth`` (and the boundary
    "prompt/skill never leave the harness" is literally true). Robust to keyword OR positional args.
    Raises if the file/JOBS can't be found, so a packaging mistake fails the synth loudly rather than
    silently creating zero schedules.
    """
    tree = ast.parse(jobs_file.read_text())
    fields = ["name", "schedule", "prompt", "skill", "session_prefix", "enabled"]
    keep = {"name", "schedule", "enabled"}  # the only fields we evaluate + emit
    jobs: list[dict[str, object]] = []

    for node in ast.walk(tree):
        # JOBS may be a plain assignment or an annotated one (``JOBS: list[...] = [...]``).
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "JOBS" for t in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "JOBS"
        ):
            value = node.value
        else:
            continue
        if not isinstance(value, ast.List):
            continue
        for elt in value.elts:
            if not (isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "ScheduledJob"):
                continue
            data: dict[str, object] = {}
            for i, arg in enumerate(elt.args):  # positional
                if i < len(fields) and fields[i] in keep:
                    data[fields[i]] = ast.literal_eval(arg)
            for kw in elt.keywords:  # keyword
                if kw.arg in keep:
                    data[kw.arg] = ast.literal_eval(kw.value)
            jobs.append(
                {
                    "name": data["name"],
                    "schedule": data["schedule"],
                    "enabled": bool(data.get("enabled", True)),
                }
            )
        if jobs:
            return jobs
    raise ValueError(f"no ScheduledJob entries found in {jobs_file}")


class SchedulerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        runtime_arn: str,
        secret_arn: str | None = None,
        poller_asset: str | None = None,
        all_enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        asset_path = Path(poller_asset) if poller_asset else _DEFAULT_ASSET
        if not asset_path.is_dir():
            raise FileNotFoundError(
                f"scheduled-invoker Lambda asset not found at {asset_path}. Build it first:\n"
                "  infra/scripts/build-poller-package.sh --local infra/build/poller\n"
                "(the scheduler reuses the same package as the poller) or pass -c poller_asset=<dir>."
            )

        env_vars = {"STRANDLY_RUNTIME_ARN": runtime_arn}
        if secret_arn:
            env_vars["STRANDLY_SECRETS_ARN"] = secret_arn

        log_group = logs.LogGroup(
            self,
            "InvokerLogGroup",
            log_group_name=f"/aws/lambda/{naming.scheduler_function}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        invoker = lambda_.Function(
            self,
            "InvokerFunction",
            function_name=naming.scheduler_function,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="strandly_harness.ops.lambdas.scheduled.invoker.lambda_handler",
            code=lambda_.Code.from_asset(str(asset_path)),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment=env_vars,
            log_group=log_group,
        )

        invoker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn, f"{runtime_arn}/*"],
            )
        )
        if secret_arn:
            invoker.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"], resources=[secret_arn]
                )
            )

        # One scheduler role reused by every schedule (each may invoke only this Lambda).
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        invoker.grant_invoke(scheduler_role)

        # One schedule per job, reading the registry statically from jobs.py.
        for job in load_jobs():
            name = str(job["name"])
            enabled = all_enabled and bool(job["enabled"])
            scheduler.CfnSchedule(
                self,
                f"Schedule-{name}",
                name=naming.schedule_name(name),
                state="ENABLED" if enabled else "DISABLED",
                schedule_expression=str(job["schedule"]),
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=invoker.function_arn,
                    role_arn=scheduler_role.role_arn,
                    # The schedule tells the one Lambda which job to run.
                    input=f'{{"job": "{name}"}}',
                ),
            )

        CfnOutput(self, "InvokerFunctionArn", value=invoker.function_arn)
