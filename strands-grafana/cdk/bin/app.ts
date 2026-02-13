#!/usr/bin/env node
import "source-map-support/register";
import * as dotenv from "dotenv";
import * as cdk from "aws-cdk-lib";
import { StrandsGrafanaStack } from "../lib/strands-grafana-stack";

// Load environment variables from .env file (if present)
dotenv.config();

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: "us-west-2",
};

new StrandsGrafanaStack(app, "StrandsGrafanaStack", {
  env,
  description:
    "Strands Grafana — GitHub metrics collection and dashboards for strands-agents org",
  tags: {
    Project: "strands-grafana",
  },
});
