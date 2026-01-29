#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { DashboardStack } from "../lib/dashboard-stack";
import { EvalPipelineStack } from "../lib/eval-pipeline-stack";

const app = new cdk.App();

// Both stacks must be in us-east-1 because:
// - Lambda@Edge requires deployment in us-east-1
// - CloudFront OAC works best with same-region resources
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: "us-east-1",
};

// Dashboard Stack: S3 + CloudFront + Lambda@Edge for basic auth
const dashboardStack = new DashboardStack(app, "DashboardStack", {
  env,
  description: "Strands Evals Dashboard - S3, CloudFront, and Lambda@Edge",
  tags: {
    Project: "strands-evals-dashboard",
  },
});

// Eval Pipeline Stack: SQS + Lambda + Secrets Manager
new EvalPipelineStack(app, "EvalPipelineStack", {
  env,
  description: "Strands Evals Pipeline - SQS, Lambda, and Secrets Manager",
  tags: {
    Project: "strands-evals-dashboard",
  },
  dashboardBucket: dashboardStack.bucket,
  distributionId: dashboardStack.distribution.distributionId,
});
