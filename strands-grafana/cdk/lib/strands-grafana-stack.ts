import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import * as path from "path";

export class StrandsGrafanaStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Secrets Manager ──────────────────────────────────────────────────
    // The GitHub PAT must already exist in Secrets Manager as a plain-text
    // secret. Pass the ARN via the GITHUB_SECRET_ARN env var or CDK context.
    const secretArn =
      process.env.GITHUB_SECRET_ARN ??
      this.node.tryGetContext("githubSecretArn");

    if (!secretArn) {
      throw new Error(
        "GITHUB_SECRET_ARN environment variable or 'githubSecretArn' CDK context must be set.\n" +
          "Create the secret first:\n" +
          '  aws secretsmanager create-secret --name strands-grafana/github-token --secret-string "ghp_xxx" --region us-west-2'
      );
    }

    const githubSecret = secretsmanager.Secret.fromSecretCompleteArn(
      this,
      "GitHubTokenSecret",
      secretArn
    );

    // ── VPC ──────────────────────────────────────────────────────────────
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 1,
    });

    // ── EFS (persistent storage for metrics.db) ─────────────────────────
    const fileSystem = new efs.FileSystem(this, "MetricsFs", {
      vpc,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      performanceMode: efs.PerformanceMode.GENERAL_PURPOSE,
      lifecyclePolicy: efs.LifecyclePolicy.AFTER_30_DAYS,
      encrypted: true,
    });

    const accessPoint = fileSystem.addAccessPoint("GrafanaData", {
      path: "/grafana-data",
      createAcl: {
        ownerUid: "0",
        ownerGid: "0",
        permissions: "755",
      },
      posixUser: {
        uid: "0",
        gid: "0",
      },
    });

    // ── ECS Cluster ─────────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc,
      containerInsights: true,
    });

    // ── Task Definition ─────────────────────────────────────────────────
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    // Mount EFS volume
    taskDef.addVolume({
      name: "metrics-data",
      efsVolumeConfiguration: {
        fileSystemId: fileSystem.fileSystemId,
        transitEncryption: "ENABLED",
        authorizationConfig: {
          accessPointId: accessPoint.accessPointId,
          iam: "ENABLED",
        },
      },
    });

    // Grant EFS access to the task role
    fileSystem.grant(
      taskDef.taskRole,
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess"
    );

    // Container definition — built from the unified Dockerfile
    const container = taskDef.addContainer("grafana", {
      image: ecs.ContainerImage.fromAsset(
        path.join(__dirname, "../../"),
        {
          file: "docker/Dockerfile",
        }
      ),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "strands-grafana",
        logRetention: logs.RetentionDays.TWO_WEEKS,
      }),
      portMappings: [{ containerPort: 3000 }],
      secrets: {
        GITHUB_TOKEN: ecs.Secret.fromSecretsManager(githubSecret),
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "wget -qO- http://localhost:3000/api/health || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(120),
      },
    });

    container.addMountPoints({
      sourceVolume: "metrics-data",
      containerPath: "/var/lib/grafana/data",
      readOnly: false,
    });

    // ── Fargate Service + ALB ───────────────────────────────────────────
    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      platformVersion: ecs.FargatePlatformVersion.LATEST,
    });

    // Allow the service to reach EFS
    service.connections.allowTo(fileSystem, ec2.Port.tcp(2049), "EFS access");

    // Application Load Balancer
    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc,
      internetFacing: true,
    });

    const listener = alb.addListener("HttpListener", {
      port: 80,
    });

    listener.addTargets("GrafanaTarget", {
      port: 3000,
      targets: [service],
      healthCheck: {
        path: "/api/health",
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // ── Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "AlbUrl", {
      value: `http://${alb.loadBalancerDnsName}`,
      description: "Grafana dashboard URL (ALB)",
    });

    new cdk.CfnOutput(this, "EfsFileSystemId", {
      value: fileSystem.fileSystemId,
      description: "EFS file system ID (persistent metrics.db storage)",
    });

    new cdk.CfnOutput(this, "ClusterArn", {
      value: cluster.clusterArn,
      description: "ECS cluster ARN",
    });

    new cdk.CfnOutput(this, "CreateSecretCommand", {
      value:
        'aws secretsmanager create-secret --name strands-grafana/github-token --secret-string "ghp_xxx" --region us-west-2',
      description: "Command to create the GitHub token secret (one-time)",
    });
  }
}
