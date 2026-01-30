import * as cdk from "aws-cdk-lib";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as logs from "aws-cdk-lib/aws-logs";
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha";
import { Construct } from "constructs";
import * as path from "path";

export interface EvalPipelineStackProps extends cdk.StackProps {
  dashboardBucket: s3.IBucket;
  distributionId: string;
}

export class EvalPipelineStack extends cdk.Stack {
  public readonly queue: sqs.Queue;
  public readonly evalFunction: PythonFunction;
  public readonly secret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: EvalPipelineStackProps) {
    super(scope, id, props);

    // SQS Queue for triggering evaluations
    this.queue = new sqs.Queue(this, "EvalTriggerQueue", {
      queueName: "strands-evals-trigger",
      visibilityTimeout: cdk.Duration.minutes(15),
      retentionPeriod: cdk.Duration.hours(24),
      receiveMessageWaitTime: cdk.Duration.seconds(20),
    });

    // Add resource-based policy to allow AWS accounts to send messages
    // Set GITHUB_ACTIONS_ACCOUNT_IDS in .env file (comma-separated for multiple accounts)
    const accountIdsEnv = process.env.GITHUB_ACTIONS_ACCOUNT_IDS;
    if (accountIdsEnv) {
      const accountIds = accountIdsEnv.split(",").map((id) => id.trim()).filter(Boolean);
      if (accountIds.length > 0) {
        this.queue.addToResourcePolicy(
          new iam.PolicyStatement({
            sid: "AllowGitHubActionsSendMessage",
            effect: iam.Effect.ALLOW,
            principals: accountIds.map((id) => new iam.AccountPrincipal(id)),
            actions: ["sqs:SendMessage"],
            resources: [this.queue.queueArn],
          })
        );
      }
    }

    // Secrets Manager Secret for Langfuse credentials
    /**
     * After deployment populate the values (in the Console or via the below command)
     * aws secretsmanager put-secret-value \
     * --secret-id strands-evals/langfuse \
     * --secret-string '{"LANGFUSE_SECRET_KEY":"actual-key",...}'
     */
    this.secret = new secretsmanager.Secret(this, "LangfuseSecret", {
      secretName: "strands-evals/langfuse",
      description: "Langfuse credentials for strands-evals Lambda",
      secretObjectValue: {
        LANGFUSE_SECRET_KEY: cdk.SecretValue.unsafePlainText("PLACEHOLDER"),
        LANGFUSE_PUBLIC_KEY: cdk.SecretValue.unsafePlainText("PLACEHOLDER"),
        LANGFUSE_HOST: cdk.SecretValue.unsafePlainText("PLACEHOLDER"),
      },
    });

    // Lambda Function for running evaluations
    this.evalFunction = new PythonFunction(this, "EvalRunnerFunction", {
      functionName: "strands-evals-runner",
      entry: path.join(__dirname, "../lambda/eval-runner"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler",
      index: "handler.py",
      timeout: cdk.Duration.minutes(15),
      memorySize: 512,
      description: "Runs post-hoc evaluations triggered by SQS",
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        CLOUDFRONT_DISTRIBUTION_ID: props.distributionId,
      },
      bundling: {
        assetExcludes: ['.venv'],
      },
    });

    // Grant permissions to the Lambda function
    
    // SQS permissions (handled by event source mapping)
    this.queue.grantConsumeMessages(this.evalFunction);

    // Secrets Manager read permission
    this.secret.grantRead(this.evalFunction);

    // S3 read/write permissions for dashboard bucket
    props.dashboardBucket.grantReadWrite(this.evalFunction);

    // CloudWatch Logs permissions (granted automatically by PythonFunction)

    // CloudFront invalidation permission
    this.evalFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudfront:CreateInvalidation"],
        resources: [
          `arn:aws:cloudfront::${this.account}:distribution/${props.distributionId}`,
        ],
      })
    );

    // Bedrock permissions for LLM-based evaluators
    this.evalFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: ["*"],
      })
    );

    // SQS Event Source Mapping
    this.evalFunction.addEventSource(
      new lambdaEventSources.SqsEventSource(this.queue, {
        batchSize: 1,
      })
    );

    // Outputs
    new cdk.CfnOutput(this, "QueueUrl", {
      value: this.queue.queueUrl,
      description: "SQS queue URL for triggering evaluations",
    });

    new cdk.CfnOutput(this, "QueueArn", {
      value: this.queue.queueArn,
      description: "SQS queue ARN",
    });

    new cdk.CfnOutput(this, "LambdaFunctionArn", {
      value: this.evalFunction.functionArn,
      description: "Eval runner Lambda function ARN",
    });

    new cdk.CfnOutput(this, "SecretArn", {
      value: this.secret.secretArn,
      description: "Langfuse credentials secret ARN",
    });

    new cdk.CfnOutput(this, "UpdateSecretCommand", {
      value: `aws secretsmanager put-secret-value --secret-id ${this.secret.secretName} --secret-string '{"LANGFUSE_SECRET_KEY":"...","LANGFUSE_PUBLIC_KEY":"...","LANGFUSE_HOST":"..."}'`,
      description: "Command to update Langfuse credentials",
    });

    new cdk.CfnOutput(this, "TestMessageCommand", {
      value: `aws sqs send-message --queue-url ${this.queue.queueUrl} --message-body '{"session_id":"test-session","eval_type":"github_issue"}'`,
      description: "Command to send a test message",
    });
  }
}
