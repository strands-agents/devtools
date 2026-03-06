#!/usr/bin/env node
import "source-map-support/register";
import * as dotenv from "dotenv";
import * as cdk from "aws-cdk-lib";
import { CommunityDashboardStack } from "../lib/community-dashboard-stack";

// Load environment variables from .env file (if present)
dotenv.config();

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? process.env.AWS_REGION ?? "us-west-2",
};

new CommunityDashboardStack(app, "CommunityDashboardStack", {
  env,
  description:
    "Community Dashboard — GitHub metrics collection and dashboards for strands-agents org",
  tags: {
    Project: "community-dashboard",
  },
});
