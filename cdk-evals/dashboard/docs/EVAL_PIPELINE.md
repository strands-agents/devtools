# Evaluation Pipeline Guide

This guide covers the automated evaluation pipeline that runs post-hoc evaluations on Langfuse sessions triggered by SQS messages.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  GitHub Action  │────▶│   SQS Queue     │────▶│     Lambda      │
│                 │     │ strands-evals-  │     │ strands-evals-  │
│                 │     │    trigger      │     │    runner       │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                        ┌─────────────────┐              │
                        │ Secrets Manager │──────────────┤
                        │ strands-evals/  │              │
                        │    langfuse     │              │
                        └─────────────────┘              │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   S3 Bucket     │◀────│    Lambda       │◀────│    Langfuse     │
│ strands-evals-  │     │  (evaluators)   │     │      API        │
│   dashboard     │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Flow:**
1. GitHub Action completes an agent run and sends session ID to SQS
2. SQS triggers the Lambda function
3. Lambda fetches Langfuse credentials from Secrets Manager
4. Lambda fetches the session traces from Langfuse
5. Lambda runs evaluators on the session
6. Results are exported to S3 for the dashboard

## Prerequisites

- AWS CLI configured with appropriate credentials
- Python 3.12 installed
- Access to the eval-sandbox repository (sibling to eval-dashboard)

Verify prerequisites:
```bash
aws sts get-caller-identity --region us-east-1
python3 --version  # Should return 3.12+
```

## First-Time Setup

### 1. Run Infrastructure Setup

```bash
chmod +x infrastructure/eval-pipeline-setup.sh
./infrastructure/eval-pipeline-setup.sh
```

This script will:
1. Create SQS queue `strands-evals-trigger`
2. Create Secrets Manager secret `strands-evals/langfuse` (placeholder values)
3. Create IAM role `strands-evals-lambda-role`
4. Create IAM policy with permissions for SQS, Secrets Manager, S3, CloudWatch
5. Build the Lambda deployment package (requires eval-sandbox)
6. Create Lambda function `strands-evals-runner`
7. Configure SQS trigger for the Lambda

The script is idempotent—running it again will skip resources that already exist.

### 2. Configure Langfuse Credentials

**IMPORTANT:** The setup script creates a placeholder secret. You must update it with real Langfuse credentials:

```bash
aws secretsmanager put-secret-value \
    --secret-id strands-evals/langfuse \
    --region us-east-1 \
    --secret-string '{
        "LANGFUSE_SECRET_KEY": "sk-lf-xxxxxxxx",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-xxxxxxxx",
        "LANGFUSE_HOST": "https://cloud.langfuse.com"
    }'
```

Verify the secret was set correctly:
```bash
aws secretsmanager get-secret-value \
    --secret-id strands-evals/langfuse \
    --region us-east-1 \
    --query SecretString \
    --output text | jq
```

## SQS Message Format

Send messages to the SQS queue with this JSON format:

```json
{
    "session_id": "github_issue_53_20260122_154534_0037178c",
    "eval_type": "github_issue"
}
```

**Fields:**
| Field | Required | Description |
|-------|----------|-------------|
| `session_id` | Yes | The Langfuse session ID to evaluate |
| `eval_type` | Yes | Type of evaluation to run |

**Supported `eval_type` values:**
- `github_issue` - GitHub issue agent evaluations
- `release_notes` - Release notes agent evaluations

## Testing

### Send a Test Message

```bash
# Get queue URL
QUEUE_URL=$(aws sqs get-queue-url \
    --queue-name strands-evals-trigger \
    --region us-east-1 \
    --query QueueUrl \
    --output text)

# Send test message
aws sqs send-message \
    --queue-url "$QUEUE_URL" \
    --region us-east-1 \
    --message-body '{"session_id":"your-session-id","eval_type":"github_issue"}'
```

### Check Lambda Logs

```bash
aws logs tail /aws/lambda/strands-evals-runner \
    --region us-east-1 \
    --follow
```

### Invoke Lambda Directly (for testing)

```bash
aws lambda invoke \
    --function-name strands-evals-runner \
    --region us-east-1 \
    --payload '{"Records":[{"body":"{\"session_id\":\"test-session\",\"eval_type\":\"github_issue\"}"}]}' \
    --cli-binary-format raw-in-base64-out \
    /tmp/lambda-output.json

cat /tmp/lambda-output.json | jq
```

## Updating the Lambda

When you make changes to the Lambda handler or dependencies:

### 1. Rebuild the deployment package

```bash
# Delete old zip to force rebuild
rm -f infrastructure/eval-runner-lambda/lambda-deployment.zip

# Run build script
./infrastructure/eval-runner-lambda/build.sh
```

### 2. Deploy the new code

```bash
aws lambda update-function-code \
    --function-name strands-evals-runner \
    --region us-east-1 \
    --zip-file fileb://infrastructure/eval-runner-lambda/lambda-deployment.zip
```

## Adding New Evaluation Types

To add a new evaluation type:

1. **Add the evaluation module** in `eval-sandbox/src/eval_sandbox/`

2. **Update the Lambda handler** (`infrastructure/eval-runner-lambda/handler.py`):
   - Add imports for the new evaluator
   - Update `DIRECT_MODE_EVALUATORS` if needed
   - Add routing logic in `run_direct_session_evaluation()` if the new type needs different evaluators

3. **Rebuild and deploy** the Lambda (see "Updating the Lambda" above)

4. **Update documentation** with the new `eval_type` value

## Configuration Reference

### Lambda Function
| Setting | Value |
|---------|-------|
| Function name | `strands-evals-runner` |
| Runtime | Python 3.12 |
| Handler | `handler.handler` |
| Timeout | 900 seconds (15 minutes) |
| Memory | 512 MB |

### SQS Queue
| Setting | Value |
|---------|-------|
| Queue name | `strands-evals-trigger` |
| Visibility timeout | 900 seconds |
| Message retention | 1 day (86400 seconds) |
| Long polling | 20 seconds |

### IAM Role Permissions
- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on the queue
- `secretsmanager:GetSecretValue` on `strands-evals/langfuse*`
- `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on `strands-evals-dashboard`
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

## Troubleshooting

### Lambda times out

The 15-minute timeout should be sufficient for most evaluations. If you're seeing timeouts:
- Check if Langfuse API is slow (network issues)
- Check if evaluators are running excessive LLM calls
- Consider breaking large sessions into smaller evaluations

### "No module named 'eval_sandbox'"

The Lambda deployment package must include the eval_sandbox package from the sibling repository. Verify:
```bash
# Check that eval-sandbox exists at the expected path
ls -la ../eval-sandbox/src/eval_sandbox
```

If the path is different, update `EVAL_SANDBOX_SRC` in `infrastructure/eval-runner-lambda/build.sh`.

### Secrets Manager access denied

Ensure the Lambda role has the correct policy attached:
```bash
aws iam list-attached-role-policies \
    --role-name strands-evals-lambda-role \
    --region us-east-1
```

### SQS messages not being processed

Check the event source mapping:
```bash
aws lambda list-event-source-mappings \
    --function-name strands-evals-runner \
    --region us-east-1
```

Check the queue for messages:
```bash
aws sqs get-queue-attributes \
    --queue-url $(aws sqs get-queue-url --queue-name strands-evals-trigger --region us-east-1 --query QueueUrl --output text) \
    --region us-east-1 \
    --attribute-names ApproximateNumberOfMessages
```

## Cost Estimate

For moderate usage (10-50 evaluations per day):

| Service | Estimated Monthly Cost |
|---------|----------------------|
| Lambda | < $5 (depends on execution time) |
| SQS | < $1 |
| Secrets Manager | < $1 |
| CloudWatch Logs | < $1 |
| **Total** | **~$2-8/month** |

## Cleanup

To remove all evaluation pipeline resources:

```bash
# Delete Lambda event source mapping
MAPPING_UUID=$(aws lambda list-event-source-mappings \
    --function-name strands-evals-runner \
    --region us-east-1 \
    --query "EventSourceMappings[0].UUID" \
    --output text)
aws lambda delete-event-source-mapping --uuid "$MAPPING_UUID" --region us-east-1

# Delete Lambda function
aws lambda delete-function --function-name strands-evals-runner --region us-east-1

# Delete SQS queue
QUEUE_URL=$(aws sqs get-queue-url --queue-name strands-evals-trigger --region us-east-1 --query QueueUrl --output text)
aws sqs delete-queue --queue-url "$QUEUE_URL" --region us-east-1

# Delete secret
aws secretsmanager delete-secret \
    --secret-id strands-evals/langfuse \
    --region us-east-1 \
    --force-delete-without-recovery

# Detach and delete IAM policy
aws iam detach-role-policy \
    --role-name strands-evals-lambda-role \
    --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/strands-evals-lambda-policy
aws iam delete-policy \
    --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/strands-evals-lambda-policy

# Delete IAM role
aws iam delete-role --role-name strands-evals-lambda-role
