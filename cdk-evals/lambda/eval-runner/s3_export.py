"""Export evaluation reports to S3 for the Strands Evals Dashboard.

This module provides functions to export evaluation results directly to S3,
enabling Lambda-based evaluation workflows where local file storage isn't available.
"""

import json
import logging
import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from strands_evals import Experiment
    from strands_evals.types.evaluation_report import EvaluationReport

logger = logging.getLogger(__name__)

# These values should match the deployed CDK infrastructure
BUCKET_NAME = "strands-agents-internal-evals-dashboard"
REGION = "us-east-1"
# CloudFront distribution ID is passed from CDK via environment variable
CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", "")


def export_reports_to_s3(
    reports: list["EvaluationReport"],
    experiment: "Experiment",
    run_id_prefix: str = "run",
    source: str = "langfuse_post_hoc",
) -> str:
    """Export evaluation reports to S3 for the dashboard.

    Args:
        reports: List of EvaluationReport objects from experiment.run_evaluations()
        experiment: The Experiment object containing cases and evaluators
        run_id_prefix: Prefix for the run ID (default: "run")
        source: Source identifier for the run (default: "langfuse_post_hoc")

    Returns:
        The run_id of the exported run
    """
    s3 = boto3.client("s3", region_name=REGION)

    # Create timestamped run ID
    timestamp = datetime.now()
    run_id = f"{run_id_prefix}_{timestamp.strftime('%Y-%m-%dT%H-%M-%S')}_langfuse"

    logger.info(f"Exporting run {run_id} to S3 bucket {BUCKET_NAME}")

    # Export each evaluator's report as a separate JSON file
    for report, evaluator in zip(reports, experiment.evaluators):
        evaluator_name = evaluator.get_type_name()
        s3_key = f"runs/{run_id}/eval_{evaluator_name}.json"

        # Get report as JSON string
        report_json = report.model_dump_json()

        logger.info(f"  Uploading eval_{evaluator_name}.json")
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=report_json,
            ContentType="application/json",
        )

    # Export manifest
    manifest = {
        "run_id": run_id,
        "timestamp": timestamp.isoformat(),
        "evaluators": [e.get_type_name() for e in experiment.evaluators],
        "total_cases": len(experiment.cases),
        "files": [f"eval_{e.get_type_name()}.json" for e in experiment.evaluators],
        "source": source,
    }

    manifest_key = f"runs/{run_id}/manifest.json"
    logger.info(f"  Uploading manifest.json")
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2),
        ContentType="application/json",
    )

    # Update runs_index.json
    _update_runs_index(s3, run_id, timestamp, experiment)

    # Invalidate CloudFront cache for runs_index.json
    _invalidate_cloudfront_cache()

    logger.info(f"Run '{run_id}' exported successfully to S3")
    print(f"\n✅ Results exported to S3: s3://{BUCKET_NAME}/runs/{run_id}/")

    return run_id


def _update_runs_index(
    s3,
    run_id: str,
    timestamp: datetime,
    experiment: "Experiment",
) -> None:
    """Update the runs_index.json file in S3.

    Args:
        s3: boto3 S3 client
        run_id: The run identifier
        timestamp: Run timestamp
        experiment: The Experiment object
    """
    runs_index_key = "runs_index.json"

    # Try to download existing runs_index.json
    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=runs_index_key)
        runs_index = json.loads(response["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.info("  Creating new runs_index.json")
            runs_index = {"runs": []}
        else:
            raise

    # Create new run entry
    new_entry = {
        "run_id": run_id,
        "timestamp": timestamp.isoformat(),
        "total_cases": len(experiment.cases),
        "evaluator_count": len(experiment.evaluators),
    }

    # Remove existing entry with same run_id (if re-uploading)
    runs_index["runs"] = [r for r in runs_index["runs"] if r["run_id"] != run_id]

    # Add new entry at the beginning
    runs_index["runs"].insert(0, new_entry)

    # Sort by timestamp descending
    runs_index["runs"].sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Upload updated runs_index.json
    logger.info("  Updating runs_index.json")
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=runs_index_key,
        Body=json.dumps(runs_index, indent=2),
        ContentType="application/json",
    )


def _invalidate_cloudfront_cache() -> None:
    """Invalidate CloudFront cache for runs_index.json."""
    cloudfront = boto3.client("cloudfront", region_name=REGION)

    try:
        caller_reference = f"s3-export-{uuid.uuid4().hex[:8]}"
        response = cloudfront.create_invalidation(
            DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {
                    "Quantity": 1,
                    "Items": ["/runs_index.json"],
                },
                "CallerReference": caller_reference,
            },
        )
        invalidation_id = response["Invalidation"]["Id"]
        logger.info(f"  CloudFront cache invalidation created: {invalidation_id}")
        print(f"  📡 CloudFront cache invalidation: {invalidation_id}")
    except ClientError as e:
        logger.warning(f"Failed to invalidate CloudFront cache: {e}")
