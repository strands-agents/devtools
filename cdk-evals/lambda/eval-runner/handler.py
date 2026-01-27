"""
Lambda handler for running post-hoc evaluations triggered by SQS.

Expected SQS message format:
{
    "session_id": "github_issue_53_20260122_154534_0037178c",
    "eval_type": "github_issue"  # or "release_notes"
}
"""

# OpenTelemetry SDK is installed with proper metadata (.dist-info included in zip)
# so the real package should work without manual stubbing.

import json
import os
import boto3
from pathlib import Path

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
from strands_evals.evaluators import (
    HelpfulnessEvaluator,
    HarmfulnessEvaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    ToolSelectionAccuracyEvaluator,
)

from post_hoc_eval import PostHocEvaluator
from evaluators import ConciseResponseEvaluator
from s3_export import export_reports_to_s3


# Evaluators that work without expected values (for direct session evaluation)
DIRECT_MODE_EVALUATORS = [
    HelpfulnessEvaluator,
    HarmfulnessEvaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    ToolSelectionAccuracyEvaluator,
    ConciseResponseEvaluator,
]


def extract_input_output_from_session(session):
    """Extract user input and agent output from a Langfuse session."""
    user_input = ""
    agent_output = ""
    
    for trace in session.traces:
        for span in trace.spans:
            span_type = type(span).__name__
            
            if span_type == "AgentInvocationSpan":
                agent_output = getattr(span, "agent_response", "") or ""
                user_prompt = getattr(span, "user_prompt", "") or ""
                if user_prompt and not user_input:
                    user_input = user_prompt
            
            if not user_input and hasattr(span, "input") and span.input:
                user_input = str(span.input)
            if not agent_output and hasattr(span, "output") and span.output:
                agent_output = str(span.output)
    
    return user_input, agent_output


def create_post_hoc_task(prefetched_sessions: dict):
    """Create a task function that returns pre-fetched sessions as trajectories."""
    
    def post_hoc_task(case: Case) -> dict:
        case_name = getattr(case, "name", None)
        session_data = prefetched_sessions.get(case_name)
        
        if not session_data:
            raise ValueError(f"No pre-fetched session found for case: {case_name}")
        
        session = session_data["session"]
        
        # Extract output from AgentInvocationSpan
        output = ""
        for trace in session.traces:
            for span in trace.spans:
                if type(span).__name__ == "AgentInvocationSpan":
                    output = getattr(span, "agent_response", "") or ""
                    break
            if output:
                break
        
        if not output:
            output = session_data.get("output_preview", "")
        
        return {
            "output": output,
            "trajectory": session,
        }
    
    return post_hoc_task


def run_direct_session_evaluation(session_id: str, eval_type: str):
    """Run evaluation on a single session by ID."""
    print(f"Running direct session evaluation")
    print(f"  Session ID: {session_id}")
    print(f"  Eval type: {eval_type}")
    
    # Initialize Langfuse evaluator
    langfuse_evaluator = PostHocEvaluator()
    
    # Fetch session from Langfuse
    print("Fetching session from Langfuse...")
    try:
        session = langfuse_evaluator.fetch_session(session_id)
        trace_count = len(session.traces)
        span_count = sum(len(t.spans) for t in session.traces)
        print(f"Fetched {trace_count} traces, {span_count} spans")
    except Exception as e:
        print(f"Error fetching session: {e}")
        raise
    
    # Extract input and output
    user_input, agent_output = extract_input_output_from_session(session)
    
    if not agent_output:
        raise ValueError("Could not extract agent output from session")
    
    print(f"Extracted input preview: {user_input[:200]}..." if len(user_input) > 200 else f"Extracted input: {user_input}")
    print(f"Extracted output preview: {agent_output[:200]}..." if len(agent_output) > 200 else f"Extracted output: {agent_output}")
    
    # Create synthetic case
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
    
    # Create experiment with direct-mode evaluators
    evaluators = [e() for e in DIRECT_MODE_EVALUATORS]
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
    
    # Print results
    print("\n" + "=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)
    
    for report, evaluator in zip(reports, experiment.evaluators):
        evaluator_name = evaluator.get_type_name()
        print(f"\n📊 {evaluator_name}")
        print("-" * 40)
        print(f"Overall Score: {report.overall_score:.2f}")
        print(f"Pass Rate: {sum(report.test_passes)}/{len(report.test_passes)}")
        
        for j, (case, score, passed, reason) in enumerate(
            zip(report.cases, report.scores, report.test_passes, report.reasons)
        ):
            status = "✅" if passed else "❌"
            case_name = case.get("name", f"Case {j}")
            print(f"  {status} {case_name}: {score:.2f}")
            if reason:
                reason_preview = reason[:100] + "..." if len(reason) > 100 else reason
                print(f"      Reason: {reason_preview}")
    
    # Export to S3
    export_reports_to_s3(
        reports, 
        experiment, 
        run_id_prefix=eval_type,
        source="lambda_sqs_trigger"
    )
    
    # Calculate results
    total_passes = sum(sum(r.test_passes) for r in reports)
    total_tests = sum(len(r.test_passes) for r in reports)
    
    return {
        "session_id": session_id,
        "eval_type": eval_type,
        "total_tests": total_tests,
        "total_passes": total_passes,
        "success": total_passes == total_tests,
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
            eval_type = body.get("eval_type", "github_issue")
            
            if not session_id:
                print(f"Missing session_id in message: {body}")
                results.append({
                    "error": "Missing session_id",
                    "message": body,
                })
                continue
            
            print(f"Processing: session_id={session_id}, eval_type={eval_type}")
            
            # Run evaluation
            result = run_direct_session_evaluation(session_id, eval_type)
            results.append(result)
            
            print(f"Evaluation complete: {result}")
            
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
