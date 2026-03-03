#!/usr/bin/env node
import "source-map-support/register";
import * as dotenv from "dotenv";
import * as cdk from "aws-cdk-lib";
import { WafStack } from "../lib/waf-stack";
import { CommunityDashboardStack } from "../lib/community-dashboard-stack";

// Load environment variables from .env file (if present)
dotenv.config();

const app = new cdk.App();

const account = process.env.CDK_DEFAULT_ACCOUNT;
const region =
  process.env.CDK_DEFAULT_REGION ?? process.env.AWS_REGION ?? "us-west-2";

// WAF for CloudFront MUST be in us-east-1
const wafStack = new WafStack(app, "CommunityDashboardWafStack", {
  env: { account, region: "us-east-1" },
  crossRegionReferences: true,
  description: "WAF WebACL for Community Dashboard CloudFront distribution",
  tags: { Project: "community-dashboard" },
});

const dashboardStack = new CommunityDashboardStack(
  app,
  "CommunityDashboardStack",
  {
    env: { account, region },
    crossRegionReferences: true,
    description:
      "Community Dashboard — GitHub metrics collection and dashboards for strands-agents org",
    tags: { Project: "community-dashboard" },
    wafAclArn: wafStack.webAclArn,
  }
);

dashboardStack.addDependency(wafStack);
