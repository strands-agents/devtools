"""Generate actionable insights from evaluation results using LLM analysis.

Analyzes eval scores, reasoning, and tool usage against the agent's SOP
to produce specific improvement recommendations.
"""

import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING

from strands import Agent
from strands.models import BedrockModel

if TYPE_CHECKING:
    from strands_evals.types.evaluation_report import EvaluationReport

from eval_configs import get_eval_config

logger = logging.getLogger(__name__)

SOPS_DIR = os.path.join(os.path.dirname(__file__), "sops")

INSIGHTS_SYSTEM_PROMPT = """You are an expert at analyzing AI agent evaluation results and generating actionable improvement recommendations.

You analyze evaluation scores, evaluator reasoning, and tool usage patterns, then compare against the agent's Standard Operating Procedure (SOP) to identify specific, implementable improvements.

Your insights must be:
- Evidence-based: cite specific scores, reasoning, or tool patterns
- Actionable: suggest concrete SOP wording changes or behavioral adjustments
- Prioritized: high severity for issues causing failures, medium for inefficiencies, low for minor improvements"""

INSIGHTS_USER_PROMPT_TEMPLATE = """## Agent Type: {eval_type}
## Agent Description: {eval_description}

## Current SOP (System Prompt)
{sop_content}

## Available Tools
{available_tools}

## Agent Input (Task Given)
{agent_input}

## Agent Output (Final Response)
{agent_output}

## Evaluation Results
{eval_summaries}

## Tool Usage Summary
{trajectory_summary}

## Task
Analyze these evaluation results and generate 3-7 specific, actionable insights for improving this agent. Consider ALL aspects of the agent's configuration:

1. **SOP/System Prompt**: Are instructions clear? Missing guidance? Contradictory?
2. **Tool definitions**: Are tool descriptions adequate? Should tools be added, removed, or have their descriptions improved?
3. **Tool usage patterns**: Is the agent using the right tools? Redundant calls? Missing tools it should use?
4. **Behavioral patterns**: Response quality, verbosity, reasoning depth, error handling
5. **Efficiency**: Turn count, tool call count, unnecessary steps

For each insight, provide:
1. category: one of "sop_improvement", "tool_usage", "behavior_pattern", "efficiency"
2. severity: "high" (causing failures), "medium" (causing inefficiency), "low" (minor improvement)
3. title: a short actionable title
4. description: detailed description with evidence from the eval results
5. sop_section: the specific SOP section heading to modify (or null if not SOP-related)
6. suggested_change: exact wording to add or modify in the SOP, tool description, or agent configuration

Also provide:
- A 1-2 sentence summary of the overall analysis
- The lowest scoring evaluator name, its score, and the primary weakness it reveals

Respond with ONLY valid JSON matching this exact schema:
{{
  "summary": "string",
  "insights": [
    {{
      "category": "string",
      "severity": "string",
      "title": "string",
      "description": "string",
      "sop_section": "string or null",
      "suggested_change": "string"
    }}
  ],
  "score_analysis": {{
    "lowest_scoring_evaluator": "string",
    "lowest_score": 0.0,
    "primary_weakness": "string"
  }}
}}"""


def _load_sop(eval_type: str) -> str:
    """Load the SOP file for a given eval type."""
    config = get_eval_config(eval_type)
    if not config.sop_file:
        return "(No SOP configured for this agent type)"
    sop_path = os.path.join(SOPS_DIR, config.sop_file)
    try:
        with open(sop_path) as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"SOP file not found: {sop_path}")
        return "(SOP file not available)"


def _build_eval_summaries(
    reports: list["EvaluationReport"],
    evaluator_names: list[str],
) -> str:
    """Build a text summary of evaluation results for the prompt."""
    lines = []
    for report, name in zip(reports, evaluator_names):
        lines.append(f"### {name}")
        lines.append(f"- Overall Score: {report.overall_score:.2f}")
        lines.append(f"- Passed: {sum(report.test_passes)}/{len(report.test_passes)}")
        if report.reasons:
            lines.append(f"- Reasoning: {report.reasons[0][:1000]}")
        lines.append("")
    return "\n".join(lines)


def _build_trajectory_summary(eval_data: dict) -> str:
    """Build a summary of tool usage from the evaluation data."""
    session = eval_data.get("actual_trajectory")
    if not session:
        return "(No trajectory data available)"

    tool_counts: dict[str, int] = {}
    if isinstance(session, dict) and "traces" in session:
        for trace in session["traces"]:
            for span in trace.get("spans", []):
                if span.get("span_type") == "execute_tool":
                    tool_name = span.get("tool_call", {}).get("name", "unknown")
                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
    elif isinstance(session, list):
        for tool_name in session:
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

    if not tool_counts:
        return "(No tool usage detected)"

    lines = [f"- {name}: {count}x" for name, count in sorted(tool_counts.items(), key=lambda x: -x[1])]
    lines.insert(0, f"Total tool calls: {sum(tool_counts.values())}")
    return "\n".join(lines)


def _extract_available_tools(eval_data: dict) -> str:
    """Extract tool definitions from AgentInvocationSpan in the trace data."""
    session = eval_data.get("actual_trajectory")
    if not isinstance(session, dict) or "traces" not in session:
        return "(No tool definitions available)"

    tools: dict[str, str] = {}
    for trace in session["traces"]:
        for span in trace.get("spans", []):
            if span.get("span_type") == "invoke_agent":
                for tool in span.get("available_tools", []):
                    name = tool.get("name", "unknown")
                    desc = tool.get("description", "")
                    if name not in tools:
                        tools[name] = desc

    if not tools:
        return "(No tool definitions found in traces)"

    lines = []
    for name, desc in sorted(tools.items()):
        if desc:
            lines.append(f"- **{name}**: {desc[:200]}")
        else:
            lines.append(f"- **{name}**")
    return "\n".join(lines)


def _extract_agent_io(eval_data: dict) -> tuple[str, str]:
    """Extract the agent's input (user_prompt) and output (agent_response) from traces."""
    session = eval_data.get("actual_trajectory")
    if not isinstance(session, dict) or "traces" not in session:
        return "(No input available)", "(No output available)"

    user_prompt = ""
    agent_response = ""
    for trace in session["traces"]:
        for span in trace.get("spans", []):
            if span.get("span_type") == "invoke_agent":
                if not user_prompt:
                    user_prompt = span.get("user_prompt", "")
                # Take the last agent response
                agent_response = span.get("agent_response", "")

    return (
        user_prompt[:2000] if user_prompt else "(No input available)",
        agent_response[:2000] if agent_response else "(No output available)",
    )


def _create_insights_agent() -> Agent:
    """Create a Strands Agent configured for insights generation."""
    return Agent(system_prompt=INSIGHTS_SYSTEM_PROMPT)


def generate_insights(
    reports: list["EvaluationReport"],
    eval_data: dict,
    eval_type: str,
) -> dict:
    """Generate actionable insights from evaluation results.

    Args:
        reports: List of EvaluationReport objects from experiment.run_evaluations()
        eval_data: The evaluation data dict (contains actual_trajectory, etc.)
        eval_type: The evaluation type (e.g., "reviewer", "github_issue")

    Returns:
        Insights dict ready for S3 export
    """
    config = get_eval_config(eval_type)
    evaluator_names = [e.__name__ for e in config.evaluators]

    sop_content = _load_sop(eval_type)
    eval_summaries = _build_eval_summaries(reports, evaluator_names)
    trajectory_summary = _build_trajectory_summary(eval_data)
    available_tools = _extract_available_tools(eval_data)
    agent_input, agent_output = _extract_agent_io(eval_data)

    user_prompt = INSIGHTS_USER_PROMPT_TEMPLATE.format(
        eval_type=eval_type,
        eval_description=config.description,
        sop_content=sop_content,
        available_tools=available_tools,
        agent_input=agent_input,
        agent_output=agent_output,
        eval_summaries=eval_summaries,
        trajectory_summary=trajectory_summary,
    )

    logger.info("Invoking insights agent...")
    agent = _create_insights_agent()
    result = agent(user_prompt)
    response_text = str(result)

    # Parse JSON from response (handle markdown code fences)
    json_text = response_text.strip()
    if json_text.startswith("```"):
        json_text = json_text.split("\n", 1)[1]
        json_text = json_text.rsplit("```", 1)[0]

    insights_data = json.loads(json_text)

    # Add metadata
    insights_data["run_id"] = ""  # Will be set by caller
    insights_data["agent_type"] = eval_type
    insights_data["timestamp"] = datetime.now().isoformat()

    logger.info(f"Generated {len(insights_data.get('insights', []))} insights")
    return insights_data
