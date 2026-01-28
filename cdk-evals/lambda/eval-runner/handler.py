"""
Lambda handler for running post-hoc evaluations triggered by SQS.

Expected SQS message format:
{
    "session_id": "github_issue_53_20260122_154534_0037178c",
    "eval_type": "github_issue"  # one of: release_notes, reviewer, implementer, ...
}

See eval_configs.py for the mapping of eval_type to evaluators.
"""


import json
import os
import boto3

# Set up environment from Secrets Manager before imports
def get_langfuse_secrets():
    """Fetch Langfuse credentials from Secrets Manager."""
    client = boto3.client("secretsmanager", region_name="us-east-1")
    secret_value = client.get_secret_value(SecretId="strands-evals/langfuse")
    return json.loads(secret_value["SecretString"])


def setup_environment():
    """Set up environment variables from Secrets Manager."""
    secrets = get_langfuse_secrets()
    os.environ["LANGFUSE_SECRET_KEY"] = secrets["LANGFUSE_SECRET_KEY"]
    os.environ["LANGFUSE_PUBLIC_KEY"] = secrets["LANGFUSE_PUBLIC_KEY"]
    os.environ["LANGFUSE_HOST"] = secrets["LANGFUSE_HOST"]
    print(f"Configured Langfuse host: {secrets['LANGFUSE_HOST']}")


# Initialize environment on cold start
setup_environment()

# Now import evaluation modules after environment is set
from strands_evals import Case, Experiment

from mappers import LangfuseSessionMapper, SessionMapper
from s3_export import export_reports_to_s3
from eval_configs import get_eval_config


def create_post_hoc_task(prefetched_sessions: dict):
    """Create a task function that returns pre-fetched sessions as trajectories."""
    
    def post_hoc_task(case: Case) -> dict:
        case_name = case.name
        session_data = prefetched_sessions.get(case_name)        
        session = session_data["session"]
        _, output = SessionMapper.extract_input_output(session)
        
        if not output:
            output = session_data.get("output_preview", "")
        
        return {
            "output": output,
            "trajectory": session,
        }
    
    return post_hoc_task


def run_session_evaluation(session_id: str, eval_type: str):
    """Run evaluation on a single session by ID."""
    print(f"Running direct session evaluation")
    print(f"  Session ID: {session_id}")
    print(f"  Eval type: {eval_type}")
    
    mapper = LangfuseSessionMapper()
    try:
        session = mapper.get_session(session_id)
    except Exception as e:
        print(f"Error fetching session: {e}")
        raise
    
    user_input, agent_output = SessionMapper.extract_input_output(session)
    if not agent_output:
        raise ValueError("Could not extract agent output from session")
    
    case_name = f"Session {session_id}"
    case_data = {
        "name": case_name,
        "input": user_input,
        "expected_output": "",
        "expected_trajectory": [],
        "metadata": {
            "session_id": session_id,
            "eval_type": eval_type,
            "direct_mode": True,
        },
    }
    
    # Create experiment with evaluators for this eval_type
    config = get_eval_config(eval_type)
    evaluators = [e() for e in config.evaluators]
    experiment = Experiment(cases=[Case(**case_data)], evaluators=evaluators)
    
    print(f"Evaluators: {[e.get_type_name() for e in experiment.evaluators]}")
    
    # Pre-fetch session data
    prefetched_sessions = {
        case_name: {
            "session": session,
            "session_id": session_id,
            "output_preview": agent_output,
        }
    }
    
    # Create task function and run evaluations
    task_fn = create_post_hoc_task(prefetched_sessions)
    
    print("Running evaluations...")
    reports = experiment.run_evaluations(task_fn)

    # Export to S3
    export_reports_to_s3(
        reports, 
        experiment, 
        run_id_prefix=eval_type,
        source="lambda_sqs_trigger"
    )

    return {
        "session_id": session_id,
        "eval_type": eval_type,
        "total_tests": sum(len(r.test_passes) for r in reports),
        "total_passes": sum(sum(r.test_passes) for r in reports)
    }


def handler(event, context):
    """Lambda handler for SQS-triggered evaluations."""
    print(f"Received event: {json.dumps(event)}")
    
    results = []
    
    for record in event.get("Records", []):
        try:
            # Parse SQS message body
            body = json.loads(record["body"])
            session_id = body.get("session_id")
            eval_type = body.get("eval_type")
            
            if not session_id:
                results.append({
                    "error": "Missing session_id",
                    "message": body,
                })
                continue

            # Run evaluation
            result = run_session_evaluation(session_id, eval_type)
            results.append(result)
            
        except Exception as e:
            print(f"Error processing record: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "error": str(e),
                "record": record,
            })
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Evaluation complete",
            "results": results,
        })
    }
