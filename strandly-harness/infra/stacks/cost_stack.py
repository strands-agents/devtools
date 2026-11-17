"""CostStack — AWS-native Bedrock/strandly cost monitoring (no fabricated token-derived metric).

Per the explicit design decision on issue #356: we do **not** invent a dollar metric by multiplying
token counts by a hardcoded price (that would be wrong the moment prompt caching, KB embeddings, or
Code Interpreter enter the bill). Instead we use **real billing data** via AWS Cost Anomaly
Detection — ML over actual spend — scoped to a cost-allocation tag so it segments *this* deployment
as a cost center:

- **AnomalyMonitor (CUSTOM, tag-scoped).** Monitors spend tagged ``{tag_key}={tag_value}`` (default
  ``app=strandly``). A tag-scoped monitor catches every strandly cost — Bedrock model invocations,
  KB embeddings, Code Interpreter, DynamoDB, Lambda — not just one service.
- **AnomalySubscription (DAILY email).** Routes a detected anomaly above the threshold to
  ``alarm_email``. (Cost Anomaly Detection email subscriptions are DAILY/WEEKLY; IMMEDIATE requires
  an SNS subscriber. We use DAILY email — operationally simplest.)

**Prerequisite (one-time, account-global, NOT CloudFormation-able):** the ``{tag_key}``
cost-allocation tag must be *activated* in the Billing console before Cost Explorer / anomaly
detection can segment on it. The app applies the tag to every resource (``cdk.Tags`` in ``app.py``),
but activation is a manual billing-console step — see ``docs/monitoring.md``. Until it's activated
the monitor still deploys; it just can't attribute spend to the tag.

Gated on ``-c alarm_email=…`` (a subscription needs a destination), so it's only synthesized when an
alert destination exists.

Note: Cost Anomaly Detection is a **global** service; deploy this stack in your home region — AWS
manages the resources globally regardless of the stack's region.
"""

from __future__ import annotations

import json

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ce as ce
from constructs import Construct

from .common import Naming


class CostStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        naming: Naming,
        alarm_email: str,
        tag_key: str = "app",
        tag_value: str = "strandly",
        threshold_dollars: float = 50.0,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # A CUSTOM monitor scoped to the strandly cost-allocation tag (the "cost center").
        monitor_spec = json.dumps(
            {"Tags": {"Key": tag_key, "Values": [tag_value], "MatchOptions": ["EQUALS"]}}
        )
        monitor = ce.CfnAnomalyMonitor(
            self,
            "CostAnomalyMonitor",
            monitor_name=f"{naming.hyphen}-cost",
            monitor_type="CUSTOM",
            monitor_specification=monitor_spec,
        )

        # Alert on any anomaly whose absolute impact is >= threshold_dollars. ThresholdExpression is
        # the current (non-deprecated) API; ANOMALY_TOTAL_IMPACT_ABSOLUTE keeps it a plain dollar
        # floor rather than a percentage.
        threshold_expr = json.dumps(
            {
                "Dimensions": {
                    "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
                    "Values": [str(threshold_dollars)],
                    "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
                }
            }
        )
        subscription = ce.CfnAnomalySubscription(
            self,
            "CostAnomalySubscription",
            # CE subscription names reject hyphens — use the underscore-only ``under`` form
            # (``strandly_dev``), the same convention as AgentCore Memory/CI, not ``hyphen``.
            subscription_name=f"{naming.under}_cost",
            frequency="DAILY",
            monitor_arn_list=[monitor.attr_monitor_arn],
            subscribers=[
                ce.CfnAnomalySubscription.SubscriberProperty(address=alarm_email, type="EMAIL")
            ],
            threshold_expression=threshold_expr,
        )
        subscription.add_dependency(monitor)

        CfnOutput(self, "CostAnomalyMonitorArn", value=monitor.attr_monitor_arn)
