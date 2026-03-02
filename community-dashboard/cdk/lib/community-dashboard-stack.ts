import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as efs from "aws-cdk-lib/aws-efs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import * as path from "path";

export class CommunityDashboardStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Secrets Manager ──────────────────────────────────────────────────
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

    // ── ECS Cluster + Cloud Map namespace ───────────────────────────────
    const namespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "Namespace",
      {
        name: "community-dashboard.local",
        vpc,
      }
    );

    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc,
      containerInsights: true,
    });

    // Helper: add EFS volume + mount to a task definition
    const addEfsVolume = (taskDef: ecs.FargateTaskDefinition) => {
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
      fileSystem.grant(
        taskDef.taskRole,
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
        "elasticfilesystem:ClientRootAccess"
      );
    };

    // ═════════════════════════════════════════════════════════════════════
    // 1. Grafana Service (always-on) — just serves dashboards
    // ═════════════════════════════════════════════════════════════════════
    const grafanaTaskDef = new ecs.FargateTaskDefinition(this, "GrafanaTaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });
    addEfsVolume(grafanaTaskDef);

    const grafanaContainer = grafanaTaskDef.addContainer("grafana", {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, "../../"), {
        file: "docker/Dockerfile.grafana",
        platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
      }),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "grafana",
        logRetention: logs.RetentionDays.TWO_WEEKS,
      }),
      portMappings: [{ containerPort: 3000 }],
      healthCheck: {
        command: [
          "CMD-SHELL",
          "wget -qO- http://localhost:3000/api/health || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    grafanaContainer.addMountPoints({
      sourceVolume: "metrics-data",
      containerPath: "/var/lib/grafana/data",
      readOnly: false,
    });

    const grafanaService = new ecs.FargateService(this, "GrafanaService", {
      cluster,
      taskDefinition: grafanaTaskDef,
      desiredCount: 1,
      assignPublicIp: false,
      platformVersion: ecs.FargatePlatformVersion.LATEST,
      enableExecuteCommand: true,
      cloudMapOptions: {
        cloudMapNamespace: namespace,
        name: "grafana",
        containerPort: 3000,
        dnsRecordType: servicediscovery.DnsRecordType.SRV,
      },
    });

    grafanaService.connections.allowTo(fileSystem, ec2.Port.tcp(2049), "EFS access");

    // ═════════════════════════════════════════════════════════════════════
    // 2. Metrics Task (on-demand + scheduled) — syncs data to EFS
    // ═════════════════════════════════════════════════════════════════════
    const metricsTaskDef = new ecs.FargateTaskDefinition(this, "MetricsTaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });
    addEfsVolume(metricsTaskDef);

    const metricsContainer = metricsTaskDef.addContainer("metrics", {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, "../../"), {
        file: "docker/Dockerfile.metrics",
        platform: cdk.aws_ecr_assets.Platform.LINUX_AMD64,
      }),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "metrics-sync",
        logRetention: logs.RetentionDays.TWO_WEEKS,
      }),
      secrets: {
        GITHUB_TOKEN: ecs.Secret.fromSecretsManager(githubSecret),
      },
    });

    metricsContainer.addMountPoints({
      sourceVolume: "metrics-data",
      containerPath: "/var/lib/grafana/data",
      readOnly: false,
    });

    // Security group for the metrics task (needs EFS access)
    const metricsSg = new ec2.SecurityGroup(this, "MetricsSg", {
      vpc,
      description: "Security group for metrics sync task",
      allowAllOutbound: true,
    });
    fileSystem.connections.allowFrom(metricsSg, ec2.Port.tcp(2049), "Metrics task EFS access");

    // EventBridge scheduled rule: daily sync at 06:00 UTC
    new events.Rule(this, "DailySyncRule", {
      schedule: events.Schedule.cron({ hour: "6", minute: "0" }),
      targets: [
        new targets.EcsTask({
          cluster,
          taskDefinition: metricsTaskDef,
          subnetSelection: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          securityGroups: [metricsSg],
          platformVersion: ecs.FargatePlatformVersion.LATEST,
        }),
      ],
    });

    // ═════════════════════════════════════════════════════════════════════
    // 3. API Gateway + CloudFront (unchanged)
    // ═════════════════════════════════════════════════════════════════════
    const vpcLink = new apigwv2.CfnVpcLink(this, "VpcLink", {
      name: "community-dashboard-vpc-link",
      subnetIds: vpc.privateSubnets.map((s) => s.subnetId),
      securityGroupIds: [grafanaService.connections.securityGroups[0].securityGroupId],
    });

    const httpApi = new apigwv2.CfnApi(this, "HttpApi", {
      name: "community-dashboard-api",
      protocolType: "HTTP",
      description: "API Gateway for Community Dashboard (Grafana)",
    });

    const integration = new apigwv2.CfnIntegration(this, "Integration", {
      apiId: httpApi.ref,
      integrationType: "HTTP_PROXY",
      integrationMethod: "ANY",
      connectionType: "VPC_LINK",
      connectionId: vpcLink.ref,
      integrationUri: grafanaService.cloudMapService!.serviceArn,
      payloadFormatVersion: "1.0",
    });

    new apigwv2.CfnRoute(this, "DefaultRoute", {
      apiId: httpApi.ref,
      routeKey: "$default",
      target: `integrations/${integration.ref}`,
    });

    new apigwv2.CfnStage(this, "DefaultStage", {
      apiId: httpApi.ref,
      stageName: "$default",
      autoDeploy: true,
    });

    grafanaService.connections.allowFrom(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.tcp(3000),
      "Allow API Gateway VPC Link"
    );

    const apiDomain = cdk.Fn.select(
      2,
      cdk.Fn.split("/", httpApi.attrApiEndpoint)
    );

    // ── WAF rate-limit rule (protect against runaway CloudFront/API GW costs)
    // Blocks any single IP exceeding 300 requests per 5-minute window.
    // WAF for CloudFront must be in us-east-1, so we use a CfnWebACL directly.
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

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      webAclId: waf.attrArn,
      defaultBehavior: {
        origin: new origins.HttpOrigin(apiDomain, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      },
    });

    // ── Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "GrafanaUrl", {
      value: `https://${distribution.distributionDomainName}`,
      description: "Grafana dashboard URL (HTTPS via CloudFront)",
    });

    new cdk.CfnOutput(this, "ApiGatewayUrl", {
      value: httpApi.attrApiEndpoint,
      description: "API Gateway endpoint URL",
    });

    new cdk.CfnOutput(this, "EfsFileSystemId", {
      value: fileSystem.fileSystemId,
      description: "EFS file system ID (persistent metrics.db storage)",
    });

    new cdk.CfnOutput(this, "ClusterArn", {
      value: cluster.clusterArn,
      description: "ECS cluster ARN",
    });

    new cdk.CfnOutput(this, "MetricsTaskDefArn", {
      value: metricsTaskDef.taskDefinitionArn,
      description: "Metrics task definition ARN (for manual run-task)",
    });

    new cdk.CfnOutput(this, "RunMetricsSyncCommand", {
      value: `aws ecs run-task --cluster ${cluster.clusterName} --task-definition ${metricsTaskDef.family} --launch-type FARGATE --network-configuration "awsvpcConfiguration={subnets=[${vpc.privateSubnets.map((s) => s.subnetId).join(",")}],securityGroups=[${metricsSg.securityGroupId}]}"`,
      description: "Command to manually trigger a metrics sync",
    });
  }
}
