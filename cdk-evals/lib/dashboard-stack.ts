import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";
import * as fs from "fs";
import * as path from "path";
import { execSync } from "child_process";

const SECRET_NAME = "strands-evals/dashboard-auth";

/**
 * Fetches auth credentials from Secrets Manager at synth time.
 * Creates the secret with default values if it doesn't exist.
 * Returns { username, password } or null if in CI/bootstrap mode.
 */
function fetchAuthCredentials(): { username: string; password: string } | null {
  // Skip fetching during bootstrap or when AWS credentials aren't available
  if (process.env.CDK_BOOTSTRAP || process.env.SKIP_SECRET_FETCH) {
    console.log("Skipping secret fetch (bootstrap mode)");
    return null;
  }

  try {
    // Use AWS CLI to fetch the secret (synchronous, respects AWS profile/credentials)
    const result = execSync(
      `aws secretsmanager get-secret-value --secret-id "${SECRET_NAME}" --region us-east-1 --query SecretString --output text 2>/dev/null`,
      { encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
    );
    return JSON.parse(result.trim());
  } catch {
    console.log(`Secret "${SECRET_NAME}" not found or not accessible. Using placeholders.`);
    console.log('Create the secret first with: aws secretsmanager create-secret --name "strands-evals/dashboard-auth" --secret-string \'{"username":"your-user","password":"your-pass"}\'');
    return null;
  }
}

export class DashboardStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // S3 Bucket for dashboard static assets
    this.bucket = new s3.Bucket(this, "DashboardBucket", {
      bucketName: "strands-agents-internal-evals-dashboard",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    // Fetch credentials from Secrets Manager at synth time
    const credentials = fetchAuthCredentials();
    const username = credentials?.username ?? "__PLACEHOLDER_USERNAME__";
    const password = credentials?.password ?? "__PLACEHOLDER_PASSWORD__";

    // Read the Lambda template and inject credentials
    const lambdaTemplatePath = path.join(__dirname, "../lambda/basic-auth/index.js");
    const lambdaTemplate = fs.readFileSync(lambdaTemplatePath, "utf-8");

    // Replace placeholders with actual credentials
    const lambdaCode = lambdaTemplate
      .replace("__BASIC_AUTH_USERNAME__", username)
      .replace("__BASIC_AUTH_PASSWORD__", password);

    // Lambda@Edge for Basic Authentication with injected credentials
    const basicAuthFunction = new cloudfront.experimental.EdgeFunction(
      this,
      "BasicAuthFunction",
      {
        functionName: "strands-evals-dashboard-basic-auth",
        runtime: lambda.Runtime.NODEJS_20_X,
        handler: "index.handler",
        code: lambda.Code.fromInline(lambdaCode),
        description: "Basic authentication for Strands Evals Dashboard",
      }
    );

    // CloudFront Distribution with Origin Access Control
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "Strands Evals Dashboard",
      defaultRootObject: "index.html",
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        compress: true,
        edgeLambdas: [
          {
            functionVersion: basicAuthFunction.currentVersion,
            eventType: cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
          },
        ],
      },
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(10),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: "/index.html",
          ttl: cdk.Duration.seconds(10),
        },
      ],
    });

    // Outputs
    new cdk.CfnOutput(this, "BucketName", {
      value: this.bucket.bucketName,
      description: "S3 bucket name for dashboard assets",
    });

    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
      description: "CloudFront distribution ID",
    });

    new cdk.CfnOutput(this, "DistributionDomainName", {
      value: this.distribution.distributionDomainName,
      description: "CloudFront distribution domain name",
    });

    new cdk.CfnOutput(this, "DashboardUrl", {
      value: `https://${this.distribution.distributionDomainName}`,
      description: "Dashboard URL",
    });

    new cdk.CfnOutput(this, "CreateSecretCommand", {
      value: `aws secretsmanager create-secret --name "${SECRET_NAME}" --secret-string '{"username":"your-username","password":"your-password"}'`,
      description: "Command to create dashboard auth secret (one-time)",
    });

    new cdk.CfnOutput(this, "UpdateSecretCommand", {
      value: `aws secretsmanager put-secret-value --secret-id "${SECRET_NAME}" --secret-string '{"username":"your-username","password":"your-password"}'`,
      description: "Command to update dashboard auth credentials",
    });
  }
}
