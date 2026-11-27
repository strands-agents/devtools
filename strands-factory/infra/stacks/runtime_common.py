"""Helpers shared by the runtime stacks (agent, sandbox): log retention."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Construct

#: Retention for every AgentCore runtime log group (agent + sandbox). They hold prompts, tool output
#: and sandbox command text (the agent's OTEL GenAI message-content logs land in the same group), so
#: they are bounded instead of kept forever; 30 days covers incident forensics.
RUNTIME_LOG_RETENTION = logs.RetentionDays.ONE_MONTH


def runtime_log_group_name(runtime_id: str) -> str:
    """The DEFAULT-endpoint log group AgentCore creates for a runtime (stdout + OTEL GenAI logs)."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def runtime_id_from_arn(runtime_arn: str) -> str:
    """``arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>`` -> ``<id>``."""
    return runtime_arn.rsplit("/", 1)[-1]


def bound_runtime_log_retention(
    scope: Construct, construct_id: str, runtime_id: str
) -> logs.LogRetention:
    """Put the retention policy on a runtime's service-created log group.

    AgentCore creates the group itself on the first invoke, so CDK cannot own it as a ``LogGroup``
    (creating one that already exists fails the deploy). ``LogRetention`` sets the policy through a
    small custom resource instead: idempotent, and it creates the group if it is not there yet.
    """
    return logs.LogRetention(
        scope,
        construct_id,
        log_group_name=runtime_log_group_name(runtime_id),
        retention=RUNTIME_LOG_RETENTION,
    )


#: Retention for the Lambda log groups that had none (CloudWatch's default is "never expire").
LAMBDA_LOG_RETENTION = logs.RetentionDays.ONE_MONTH


def bounded_log_group(scope: Construct, construct_id: str) -> logs.LogGroup:
    """A CFN-owned, generated-name log group with a retention, for a Lambda's ``log_group=``.

    Generated name on purpose: an explicit ``/aws/lambda/<fn>`` name would collide with the group
    Lambda already auto-created for an existing function (CloudFormation cannot adopt it). The old
    auto-created group simply stops receiving logs.
    """
    return logs.LogGroup(
        scope,
        construct_id,
        retention=LAMBDA_LOG_RETENTION,
        removal_policy=RemovalPolicy.DESTROY,
    )
