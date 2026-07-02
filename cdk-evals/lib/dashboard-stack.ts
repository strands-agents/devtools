import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
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
 * Returns { username, password } when the secret is readable. Returns null
 * only when the secret is unreadable/absent or when running in bootstrap/skip
 * mode. The secret is never created and no default values are supplied. When
 * this returns null the caller throws (fail-closed) so deployment is denied
 * unless real credentials are available.
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
    console.log(`Secret "${SECRET_NAME}" not found or not accessible.`);
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

    // Fetch credentials from Secrets Manager at synth time.
    // Deny deployment when credentials are unavailable so the dashboard is
    // never served with anything other than the configured secret values.
    const credentials = fetchAuthCredentials();
    if (!credentials?.username || !credentials?.password) {
      throw new Error(
        `Dashboard auth credentials are unavailable. Create the secret "${SECRET_NAME}" before deploying:\n` +
          `aws secretsmanager create-secret --name "${SECRET_NAME}" --secret-string '{"username":"your-username","password":"your-password"}'`
      );
    }
    const { username, password } = credentials;

    // Read the Lambda template and inject credentials
    const lambdaTemplatePath = path.join(__dirname, "../lambda/basic-auth/index.js");
    const lambdaTemplate = fs.readFileSync(lambdaTemplatePath, "utf-8");

    // Replace placeholders with JSON-encoded string literals. JSON.stringify
    // safely escapes quotes, backslashes, backticks, and ${...} sequences so a
    // credential value cannot corrupt or inject code into the generated Lambda.
    // Use function replacers so "$" sequences in the JSON-encoded credential
    // are inserted literally rather than interpreted by String.replace.
    const lambdaCode = lambdaTemplate
      .replace("__BASIC_AUTH_USERNAME__", () => JSON.stringify(username))
      .replace("__BASIC_AUTH_PASSWORD__", () => JSON.stringify(password));

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

    // Deploy dashboard static assets to S3
    // Requires: npm run build in dashboard/ directory before cdk deploy
    const dashboardDistPath = path.join(__dirname, "../dashboard/dist");
    if (fs.existsSync(dashboardDistPath)) {
      new s3deploy.BucketDeployment(this, "DashboardDeployment", {
        sources: [s3deploy.Source.asset(dashboardDistPath)],
        destinationBucket: this.bucket,
        distribution: this.distribution,
        distributionPaths: ["/*"],
        // Preserve S3 evaluation data - don't overwrite runs/ or runs_index.json
        exclude: ["runs/*", "runs_index.json"],
        prune: false, // Don't delete files not in the source
      });
    } else {
      console.log("Dashboard not built. Run 'cd dashboard && npm run build' before deploy.");
    }

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
