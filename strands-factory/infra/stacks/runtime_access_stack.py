from __future__ import annotations

import json

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

INVOKE_ACTION = "bedrock-agentcore:InvokeAgentRuntime"


def endpoint_arn_of(runtime_arn: str) -> str:
    return f"{runtime_arn}/runtime-endpoint/DEFAULT"


def invoke_policy(resource_arn: str, invoker_arns: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CrossAccountInvoke",
                    "Effect": "Allow",
                    "Principal": {"AWS": list(invoker_arns)},
                    "Action": INVOKE_ACTION,
                    "Resource": resource_arn,
                }
            ],
        }
    )


class RuntimeAccessStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        invoker_arns: tuple[str, ...],
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not invoker_arns:
            raise ValueError("RuntimeAccessStack needs at least one invoker role ARN")
        endpoint_arn = endpoint_arn_of(runtime_arn)
        agentcore.CfnResourcePolicy(
            self, "RuntimeInvokePolicy", resource_arn=runtime_arn, policy=invoke_policy(runtime_arn, invoker_arns)
        )
        agentcore.CfnResourcePolicy(
            self, "EndpointInvokePolicy", resource_arn=endpoint_arn, policy=invoke_policy(endpoint_arn, invoker_arns)
        )
        CfnOutput(self, "RuntimeInvokers", value=",".join(invoker_arns))
