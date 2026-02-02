# Deployment Guide

This guide covers deploying the Strands Evals Dashboard to AWS using S3 and CloudFront.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Users     │────▶│  CloudFront  │────▶│     S3      │
│             │     │    (CDN)     │     │   Bucket    │
└─────────────┘     └──────────────┘     └─────────────┘
                           │
                    ┌──────┴──────┐
                    │    OAC      │
                    │ (Auth S3)   │
                    └─────────────┘
```

- **S3 Bucket**: Stores static build files (private, no public access)
- **CloudFront**: CDN for global delivery with HTTPS
- **Origin Access Control (OAC)**: Securely connects CloudFront to S3

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js and npm installed
- `jq` installed (for JSON parsing in scripts)

Verify prerequisites:
```bash
aws sts get-caller-identity --region us-east-1  # Should return your AWS account info
node --version                                   # Should return v18+
jq --version                                     # Should return version info
```

## First-Time Setup

### 1. Run Infrastructure Setup

```bash
chmod +x infrastructure/setup.sh
./infrastructure/setup.sh
```

This script will:
1. Create S3 bucket `strands-evals-dashboard`
2. Block all public access on the bucket
3. Create CloudFront Origin Access Control (OAC)
4. Create CloudFront distribution with SPA routing support
5. Apply bucket policy granting CloudFront access
6. Save configuration to `.deploy-config`

The script is idempotent—running it again will skip resources that already exist.

### 2. Wait for CloudFront Deployment

CloudFront distributions take 5-10 minutes to deploy. Check status:

```bash
aws cloudfront get-distribution --id <DISTRIBUTION_ID> \
    --region us-east-1 \
    --query "Distribution.Status" --output text
```

Status will change from `InProgress` to `Deployed`.

## Deploying Updates

After initial setup, deploy updates with:

```bash
chmod +x deploy.sh  # First time only
./deploy.sh
```

This will:
1. Build the application (`npm run build`)
2. Sync build files to S3
3. Invalidate CloudFront cache

## Configuration Files

| File | Purpose |
|------|---------|
| `.deploy-config` | Generated config with bucket/distribution IDs |
| `infrastructure/cloudfront-config.json` | CloudFront distribution settings |
| `infrastructure/bucket-policy.json` | S3 bucket policy template |

## Troubleshooting

### "Access Denied" when accessing the site

- Ensure CloudFront distribution status is `Deployed`
- Verify bucket policy was applied: `aws s3api get-bucket-policy --bucket strands-evals-dashboard --region us-east-1`
- Check OAC is attached to the distribution

### 403 errors on page refresh

The CloudFront distribution is configured with custom error responses that redirect 403/404 to `/index.html`. If this isn't working:

1. Check custom error responses in CloudFront console
2. Ensure `index.html` exists in the S3 bucket root

### Cache not updating after deploy

Invalidations take 1-2 minutes. Check status:

```bash
aws cloudfront list-invalidations --distribution-id <DISTRIBUTION_ID> --region us-east-1
```

For immediate testing, use browser incognito mode or add cache-busting query params.

## Cost Estimate

For internal team use with moderate traffic:

| Service | Estimated Monthly Cost |
|---------|----------------------|
| S3 Storage | < $1 (few MB) |
| S3 Requests | < $1 |
| CloudFront | $0-5 depending on traffic |
| **Total** | **~$1-5/month** |

CloudFront provides 1TB free transfer per month under the free tier.

## Cleanup

To remove all AWS resources:

```bash
# Empty and delete S3 bucket
aws s3 rm s3://strands-evals-dashboard --recursive --region us-east-1
aws s3 rb s3://strands-evals-dashboard --region us-east-1

# Delete CloudFront distribution (must disable first)
# Get the distribution ID from .deploy-config
aws cloudfront get-distribution-config --id <DISTRIBUTION_ID> --region us-east-1 > dist-config.json
# Edit dist-config.json: set "Enabled": false
# Update distribution with disabled config, then delete
aws cloudfront delete-distribution --id <DISTRIBUTION_ID> --if-match <ETAG> --region us-east-1

# Delete OAC
aws cloudfront delete-origin-access-control --id <OAC_ID> --region us-east-1
```
