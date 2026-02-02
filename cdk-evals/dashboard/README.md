# Strands Evals Dashboard

A React + TypeScript dashboard for viewing Strands evaluation results.

## Local Development

```bash
npm install
npm run dev
```

Open http://localhost:5173 in your browser.

## Deployment

### Prerequisites

- AWS CLI configured with appropriate credentials
- `jq` installed for JSON processing

### Initial Setup

Run the setup script to create all AWS infrastructure:

```bash
./infrastructure/setup.sh
```

This creates:
- S3 bucket for hosting
- CloudFront distribution
- Origin Access Control
- Lambda@Edge for Basic Auth
- All necessary IAM roles

### Deploy Updates

After making changes, deploy to production:

```bash
./deploy.sh
```

This preserves evaluation data in S3 (`runs/` and `runs_index.json`) while updating the dashboard app.

### Access

The dashboard is protected by Basic Auth:
- **Username:** `strands_evals_dashboard`
- **Password:** See `infrastructure/basic-auth-lambda.js`

### Uploading Evaluation Results

From eval-sandbox, upload run results to the shared dashboard:

```bash
# After running evaluations
python -m eval_sandbox.upload_to_dashboard ./path/to/run_directory/
```

This uploads the run to S3 and updates the runs index. All team members can then view results on the dashboard.

### Automated Evaluation Pipeline (SQS + Lambda)

For automated evaluations triggered by GitHub Actions, set up the evaluation pipeline:

```bash
./infrastructure/eval-pipeline-setup.sh
```

This creates:
- SQS queue `strands-evals-trigger` - receives session IDs to evaluate
- Lambda `strands-evals-runner` - runs post-hoc evaluations
- Secrets Manager `strands-evals/langfuse` - stores Langfuse credentials

After setup, configure Langfuse credentials:
```bash
aws secretsmanager put-secret-value \
    --secret-id strands-evals/langfuse \
    --region us-east-1 \
    --secret-string '{"LANGFUSE_SECRET_KEY":"...","LANGFUSE_PUBLIC_KEY":"...","LANGFUSE_HOST":"..."}'
```

Send messages to trigger evaluations:
```json
{"session_id": "your-langfuse-session-id", "eval_type": "github_issue"}
```

See [docs/EVAL_PIPELINE.md](docs/EVAL_PIPELINE.md) for full documentation.

### AWS Resources

All resources are tagged with `Project=strands-evals-dashboard` for easy discovery:

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=strands-evals-dashboard
```

Resources created:
- S3 bucket: `strands-evals-dashboard`
- CloudFront distribution (comment: "Strands Evals Dashboard")
- CloudFront OAC: `strands-evals-dashboard-oac`
- Lambda function: `strands-evals-dashboard-basic-auth` (Basic Auth)
- Lambda function: `strands-evals-runner` (Evaluation Pipeline)
- SQS queue: `strands-evals-trigger`
- Secrets Manager: `strands-evals/langfuse`
- IAM role: `strands-evals-dashboard-lambda-edge-role`
- IAM role: `strands-evals-lambda-role`

### Ada Burner Account

```bash
ada credentials update --account=<ACCOUNT_ID> --provider=conduit --role=IibsAdminAccess-DO-NOT-DELETE --once
```

## Tech Stack

- React 18
- TypeScript
- Vite
- AWS (S3, CloudFront, Lambda@Edge)
