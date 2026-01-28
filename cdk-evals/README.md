# Strands Evals Infrastructure (CDK)

AWS CDK infrastructure for the Strands Evals Dashboard and Evaluation Pipeline.

## Architecture

### DashboardStack
- **S3 Bucket** (`strands-agents-internal-evals-dashboard`): Hosts static dashboard assets
- **CloudFront Distribution**: CDN with HTTPS, SPA error handling
- **Lambda@Edge**: Basic authentication for dashboard access (credentials injected at deploy time)
- **Secrets Manager** (`strands-evals/dashboard-auth`): Stores dashboard auth credentials
- **Origin Access Control**: Secure S3 access via CloudFront only

### EvalPipelineStack  
- **SQS Queue** (`strands-evals-trigger`): Triggers evaluations
- **Lambda Function** (`strands-evals-runner`): Runs post-hoc evaluations
- **Secrets Manager** (`strands-evals/langfuse`): Stores Langfuse credentials

## Prerequisites

1. **Node.js** (v18+)
2. **AWS CLI** configured with credentials
3. **AWS CDK CLI**: `npm install -g aws-cdk`
4. **Docker** (required for Python Lambda bundling - must be running during deployment)

## Setup

```bash
cd cdk-evals
npm install
```

## Deployment

> **Note:** Docker must be running before deploying. The Python Lambda function uses Docker to bundle pip dependencies in a Lambda-compatible environment.

### Deploy All Stacks

```bash
npm run deploy
```

### Deploy Individual Stacks

```bash
# Dashboard only
npm run deploy:dashboard

# Eval Pipeline only (requires DashboardStack)
npm run deploy:pipeline
```

### First Deployment Steps

1. **Bootstrap CDK** (one-time per account/region):
   ```bash
   cdk bootstrap aws://ACCOUNT_ID/us-east-1
   ```

2. **Create the dashboard auth secret** (before first deploy):
   ```bash
   aws secretsmanager create-secret \
       --name strands-evals/dashboard-auth \
       --secret-string '{"username":"your-username","password":"your-secure-password"}' \
       --region us-east-1
   ```

3. **Deploy stacks** (credentials are fetched from Secrets Manager at deploy time):
   ```bash
   npm run deploy
   ```

4. **Update Langfuse credentials** (required for eval pipeline):
   ```bash
   aws secretsmanager put-secret-value \
       --secret-id strands-evals/langfuse \
       --secret-string '{"LANGFUSE_SECRET_KEY":"sk-...","LANGFUSE_PUBLIC_KEY":"pk-...","LANGFUSE_HOST":"https://your-langfuse-host.com"}'
   ```

> **Note:** Dashboard auth credentials are read from Secrets Manager at CDK synth time and injected into Lambda@Edge. If you deploy without creating the secret first, placeholder values will be used and authentication won't work.

## Stack Outputs

After deployment, the following outputs are available:

### DashboardStack
- `BucketName`: S3 bucket for dashboard assets
- `DistributionId`: CloudFront distribution ID
- `DashboardUrl`: Public URL for the dashboard

### EvalPipelineStack
- `QueueUrl`: SQS queue URL for triggering evaluations
- `LambdaFunctionArn`: Eval runner Lambda ARN
- `SecretArn`: Langfuse credentials secret ARN

## Usage

### Triggering Evaluations

Send a message to the SQS queue:

```bash
aws sqs send-message \
    --queue-url https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/strands-evals-trigger \
    --message-body '{"session_id":"your-langfuse-session-id","eval_type":"github_issue"}'
```

Supported `eval_type` values:
- `github_issue`
- `release_notes`

### Dashboard Authentication

Credentials are stored in AWS Secrets Manager (`strands-evals/dashboard-auth`) and injected into Lambda@Edge at deploy time.

**How it works:**
1. CDK reads the secret from Secrets Manager during synthesis (using AWS CLI)
2. Credentials are injected into the Lambda@Edge code
3. The Lambda is deployed with the baked-in credentials

**To update credentials:**
```bash
# 1. Update the secret
aws secretsmanager put-secret-value \
    --secret-id strands-evals/dashboard-auth \
    --secret-string '{"username":"new-username","password":"new-password"}'

# 2. Redeploy to inject new credentials into Lambda@Edge
npm run deploy:dashboard
```

> **Important:** Credentials are baked into the Lambda function at deploy time. You must redeploy after changing the secret for changes to take effect. The secret must exist before deployment or placeholder values will be used.

## Useful Commands

```bash
# Synthesize CloudFormation templates
npm run synth

# Show diff against deployed stacks
cdk diff

# Destroy all stacks
npm run destroy
```

## Lambda Code

### Basic Auth (`lambda/basic-auth/`)
Node.js Lambda@Edge function for CloudFront viewer-request authentication.

### Eval Runner (`lambda/eval-runner/`)
Python Lambda function that:
1. Receives SQS messages with session IDs
2. Fetches session data from Langfuse
3. Runs evaluations using strands-agents-evals
4. Exports results to S3

## Adding New Evaluators

To add new evaluation types:

1. Update `lambda/eval-runner/handler.py`:
   - Add new evaluator imports
   - Add to `DIRECT_MODE_EVALUATORS` list
   - Handle new `eval_type` values if needed

2. Update `lambda/eval-runner/requirements.txt` if new dependencies are needed

3. Redeploy:
   ```bash
   npm run deploy:pipeline
   ```
