import * as cdk from "aws-cdk-lib";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";

/**
 * WAF WebACL for CloudFront — must be deployed in us-east-1.
 * Exports the ACL ARN for cross-region reference by the main stack.
 */
export class WafStack extends cdk.Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const waf = new wafv2.CfnWebACL(this, "RateLimitAcl", {
      scope: "CLOUDFRONT",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: "CommunityDashboardWAF",
        sampledRequestsEnabled: false,
      },
      rules: [
        {
          name: "RateLimitPerIP",
          priority: 1,
          action: { block: {} },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "RateLimitPerIP",
            sampledRequestsEnabled: false,
          },
          statement: {
            rateBasedStatement: {
              limit: 300,
              aggregateKeyType: "IP",
            },
          },
        },
      ],
    });

    this.webAclArn = waf.attrArn;
  }
}
