"""CostStack: AWS Cost Anomaly Detection scoped to this deployment's cost-allocation tag.

Spend is judged from billing data, not estimated from token counts: a tag-scoped custom anomaly
monitor and a subscription that emails on anomalies above the configured threshold.
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
        subscription.add_resource_dependency(monitor)

        CfnOutput(self, "CostAnomalyMonitorArn", value=monitor.attr_monitor_arn)
