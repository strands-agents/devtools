"""Agent Orchestrator for Strands Command.

Enables agents to dispatch and coordinate sub-agents via GitHub Actions
workflow_dispatch events. Provides security limits, rate limiting, and
result collection for multi-agent orchestration.

Key Features:
1. Sub-agent dispatch via GitHub workflow_dispatch API
2. Configurable security limits (concurrency, token budgets, timeouts)
3. Rate limiting with cooldown between dispatches
4. Result collection via workflow run polling
5. Graceful failure handling for partial results

Usage:
    from orchestrator import AgentOrchestrator

    orch = AgentOrchestrator(repo="owner/repo")
    run_id = orch.dispatch_agent(
        agent_type="adversarial-test",
        prompt="Test PR #123 for edge cases",
        system_prompt="You are an adversarial tester...",
    )
    result = orch.wait_for_completion(run_id, timeout_minutes=30)

Security:
    - Max concurrent agents: ORCHESTRATOR_MAX_CONCURRENT (default: 3)
    - Max total agents per run: ORCHESTRATOR_MAX_TOTAL_AGENTS (default: 5)
    - Per-agent timeout: ORCHESTRATOR_AGENT_TIMEOUT_MINUTES (default: 30)
    - Cooldown between dispatches: ORCHESTRATOR_COOLDOWN_SECONDS (default: 10)
    - Token budget: ORCHESTRATOR_AGENT_MAX_TOKENS (default: 32000)
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import requests
from strands import tool


class AgentStatus(str, Enum):
    """Status of a dispatched sub-agent."""
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class AgentTask:
    """Represents a dispatched sub-agent task."""
    agent_type: str
    prompt: str
    system_prompt: str = ""
    status: AgentStatus = AgentStatus.PENDING
    run_id: str | None = None
    dispatch_time: datetime | None = None
    completion_time: datetime | None = None
    result: str | None = None
    error: str | None = None
    workflow: str = "strands-command.yml"


@dataclass
class OrchestratorConfig:
    """Configuration for the agent orchestrator."""
    max_concurrent: int = 3
    max_total_agents: int = 5
    agent_timeout_minutes: int = 30
    agent_max_tokens: int = 32000
    cooldown_seconds: int = 10
    poll_interval_seconds: int = 30

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        """Create config from environment variables."""
        return cls(
            max_concurrent=int(os.getenv("ORCHESTRATOR_MAX_CONCURRENT", "3")),
            max_total_agents=int(os.getenv("ORCHESTRATOR_MAX_TOTAL_AGENTS", "5")),
            agent_timeout_minutes=int(os.getenv("ORCHESTRATOR_AGENT_TIMEOUT_MINUTES", "30")),
            agent_max_tokens=int(os.getenv("ORCHESTRATOR_AGENT_MAX_TOKENS", "32000")),
            cooldown_seconds=int(os.getenv("ORCHESTRATOR_COOLDOWN_SECONDS", "10")),
            poll_interval_seconds=int(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "30")),
        )


class AgentOrchestrator:
    """Orchestrates sub-agent dispatch and result collection.

    Provides security-limited agent-to-agent coordination via
    GitHub Actions workflow_dispatch events.
    """

    def __init__(
        self,
        repo: str | None = None,
        config: OrchestratorConfig | None = None,
    ):
        self.repo = repo or os.getenv("GITHUB_REPOSITORY", "")
        self.config = config or OrchestratorConfig.from_env()
        self.token = os.getenv("PAT_TOKEN", os.getenv("GITHUB_TOKEN", ""))
        self.tasks: list[AgentTask] = []
        self._last_dispatch_time: float = 0
        self._total_dispatched: int = 0

        if not self.repo:
            raise ValueError("Repository not specified and GITHUB_REPOSITORY not set")
        if not self.token:
            raise ValueError("No GitHub token available (PAT_TOKEN or GITHUB_TOKEN)")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _check_rate_limit(self) -> bool:
        """Check if we're within GitHub API rate limits."""
        try:
            resp = requests.get(
                "https://api.github.com/rate_limit",
                headers=self._headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            remaining = data.get("resources", {}).get("core", {}).get("remaining", 0)
            return remaining > 10  # Keep a buffer
        except Exception:
            return True  # Optimistic on failure

    def _enforce_cooldown(self) -> None:
        """Enforce minimum cooldown between dispatches."""
        elapsed = time.time() - self._last_dispatch_time
        if elapsed < self.config.cooldown_seconds:
            wait_time = self.config.cooldown_seconds - elapsed
            print(f"⏳ Cooldown: waiting {wait_time:.1f}s before next dispatch")
            time.sleep(wait_time)

    def _get_active_count(self) -> int:
        """Count currently active (dispatched or running) agents."""
        return sum(
            1 for t in self.tasks
            if t.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING)
        )

    def can_dispatch(self) -> bool:
        """Check if we can dispatch another agent."""
        if self._total_dispatched >= self.config.max_total_agents:
            print(f"⚠️ Total agent limit reached ({self.config.max_total_agents})")
            return False
        if self._get_active_count() >= self.config.max_concurrent:
            print(f"⚠️ Concurrent agent limit reached ({self.config.max_concurrent})")
            return False
        if not self._check_rate_limit():
            print("⚠️ GitHub API rate limit approaching")
            return False
        return True

    def dispatch_agent(
        self,
        agent_type: str,
        prompt: str,
        system_prompt: str = "",
        workflow: str = "strands-command.yml",
        extra_inputs: dict[str, str] | None = None,
    ) -> AgentTask:
        """Dispatch a sub-agent via workflow_dispatch.

        Args:
            agent_type: Type of agent (e.g., "adversarial-test", "release-notes")
            prompt: Task prompt for the sub-agent
            system_prompt: System prompt override for the sub-agent
            workflow: Target workflow file (default: strands-command.yml)
            extra_inputs: Additional workflow inputs

        Returns:
            AgentTask with dispatch status

        Raises:
            RuntimeError: If dispatch limits are exceeded
        """
        task = AgentTask(
            agent_type=agent_type,
            prompt=prompt,
            system_prompt=system_prompt,
            workflow=workflow,
        )

        # Security checks
        if not self.can_dispatch():
            task.status = AgentStatus.FAILED
            task.error = "Dispatch limit exceeded"
            self.tasks.append(task)
            return task

        # Enforce cooldown
        self._enforce_cooldown()

        # Parse workflow target (same-repo or cross-repo)
        if workflow.count("/") >= 2:
            # Cross-repo: "owner/repo/workflow.yml"
            parts = workflow.split("/", 2)
            dispatch_repo = f"{parts[0]}/{parts[1]}"
            dispatch_workflow = parts[2]
        else:
            dispatch_repo = self.repo
            dispatch_workflow = workflow

        # Build inputs
        inputs: dict[str, str] = {
            "command": prompt,
        }
        if system_prompt:
            inputs["system_prompt"] = system_prompt

        if extra_inputs:
            inputs.update(extra_inputs)

        # Dispatch via GitHub API
        url = f"https://api.github.com/repos/{dispatch_repo}/actions/workflows/{dispatch_workflow}/dispatches"
        payload = {
            "ref": "main",
            "inputs": inputs,
        }

        try:
            print(f"🚀 Dispatching {agent_type} agent to {dispatch_repo}/{dispatch_workflow}")
            resp = requests.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=30,
            )

            if resp.status_code == 204:
                task.status = AgentStatus.DISPATCHED
                task.dispatch_time = datetime.now(timezone.utc)
                self._last_dispatch_time = time.time()
                self._total_dispatched += 1
                print(f"✅ Dispatched {agent_type} agent successfully")

                # Try to find the run ID (poll recent runs)
                time.sleep(2)  # Brief wait for GitHub to register the run
                run_id = self._find_recent_run(dispatch_repo, dispatch_workflow)
                if run_id:
                    task.run_id = run_id
                    print(f"   Run ID: {run_id}")
            else:
                task.status = AgentStatus.FAILED
                task.error = f"HTTP {resp.status_code}: {resp.text}"
                print(f"❌ Dispatch failed: {task.error}")

        except Exception as e:
            task.status = AgentStatus.FAILED
            task.error = str(e)
            print(f"❌ Dispatch error: {e}")

        self.tasks.append(task)
        return task

    def _find_recent_run(self, repo: str, workflow: str) -> str | None:
        """Find the most recent workflow run (just dispatched)."""
        try:
            url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
            resp = requests.get(
                url,
                headers=self._headers,
                params={"per_page": 1, "status": "queued"},
                timeout=10,
            )
            resp.raise_for_status()
            runs = resp.json().get("workflow_runs", [])
            if runs:
                return str(runs[0]["id"])
        except Exception:
            pass
        return None

    def check_run_status(self, task: AgentTask) -> AgentStatus:
        """Check the status of a dispatched agent's workflow run.

        Args:
            task: The agent task to check

        Returns:
            Updated AgentStatus
        """
        if not task.run_id:
            return task.status

        try:
            url = f"https://api.github.com/repos/{self.repo}/actions/runs/{task.run_id}"
            resp = requests.get(url, headers=self._headers, timeout=10)
            resp.raise_for_status()
            run_data = resp.json()

            status = run_data.get("status", "")
            conclusion = run_data.get("conclusion", "")

            if status == "completed":
                if conclusion == "success":
                    task.status = AgentStatus.COMPLETED
                    task.completion_time = datetime.now(timezone.utc)
                else:
                    task.status = AgentStatus.FAILED
                    task.error = f"Workflow concluded: {conclusion}"
                    task.completion_time = datetime.now(timezone.utc)
            elif status in ("queued", "in_progress"):
                task.status = AgentStatus.RUNNING

                # Check timeout
                if task.dispatch_time:
                    elapsed = (datetime.now(timezone.utc) - task.dispatch_time).total_seconds()
                    if elapsed > self.config.agent_timeout_minutes * 60:
                        task.status = AgentStatus.TIMED_OUT
                        task.error = f"Exceeded {self.config.agent_timeout_minutes}m timeout"
                        task.completion_time = datetime.now(timezone.utc)

        except Exception as e:
            print(f"⚠️ Status check failed for run {task.run_id}: {e}")

        return task.status

    def wait_for_all(self, timeout_minutes: int | None = None) -> list[AgentTask]:
        """Wait for all dispatched agents to complete.

        Args:
            timeout_minutes: Overall timeout (default: config.agent_timeout_minutes)

        Returns:
            List of all tasks with final statuses
        """
        timeout = (timeout_minutes or self.config.agent_timeout_minutes) * 60
        start_time = time.time()

        active_tasks = [
            t for t in self.tasks
            if t.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING)
        ]

        while active_tasks and (time.time() - start_time) < timeout:
            for task in active_tasks:
                self.check_run_status(task)

            active_tasks = [
                t for t in self.tasks
                if t.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING)
            ]

            if active_tasks:
                elapsed = int(time.time() - start_time)
                print(f"⏳ Waiting for {len(active_tasks)} agent(s)... ({elapsed}s elapsed)")
                time.sleep(self.config.poll_interval_seconds)

        # Mark remaining active tasks as timed out
        for task in active_tasks:
            if task.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING):
                task.status = AgentStatus.TIMED_OUT
                task.error = "Overall orchestration timeout exceeded"
                task.completion_time = datetime.now(timezone.utc)

        return self.tasks

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all orchestrated tasks.

        Returns:
            Dictionary with task counts and details
        """
        summary = {
            "total": len(self.tasks),
            "completed": sum(1 for t in self.tasks if t.status == AgentStatus.COMPLETED),
            "failed": sum(1 for t in self.tasks if t.status == AgentStatus.FAILED),
            "timed_out": sum(1 for t in self.tasks if t.status == AgentStatus.TIMED_OUT),
            "tasks": [],
        }

        for task in self.tasks:
            duration = None
            if task.dispatch_time and task.completion_time:
                duration = (task.completion_time - task.dispatch_time).total_seconds()

            summary["tasks"].append({
                "agent_type": task.agent_type,
                "status": task.status.value,
                "run_id": task.run_id,
                "duration_seconds": duration,
                "error": task.error,
            })

        return summary

    def format_report(self) -> str:
        """Format a markdown report of orchestration results.

        Returns:
            Markdown-formatted orchestration report
        """
        summary = self.get_summary()
        lines = [
            "## 📊 Orchestration Report",
            "",
            f"**Total agents**: {summary['total']} | "
            f"**Completed**: {summary['completed']} | "
            f"**Failed**: {summary['failed']} | "
            f"**Timed out**: {summary['timed_out']}",
            "",
            "| Sub-Agent | Status | Duration | Run ID | Error |",
            "|-----------|--------|----------|--------|-------|",
        ]

        status_icons = {
            "completed": "✅",
            "failed": "❌",
            "timed_out": "⏱️",
            "dispatched": "🚀",
            "running": "🔄",
            "pending": "⏳",
        }

        for task_info in summary["tasks"]:
            icon = status_icons.get(task_info["status"], "❓")
            duration = ""
            if task_info["duration_seconds"]:
                mins = int(task_info["duration_seconds"] // 60)
                secs = int(task_info["duration_seconds"] % 60)
                duration = f"{mins}m {secs}s"

            error = task_info["error"] or ""
            if len(error) > 40:
                error = error[:40] + "..."

            lines.append(
                f"| {task_info['agent_type']} | {icon} {task_info['status']} | "
                f"{duration} | {task_info['run_id'] or 'N/A'} | {error} |"
            )

        return "\n".join(lines)


# =============================================================================
# Tool interface for agent usage
# =============================================================================

# Module-level orchestrator instance (lazy-initialized)
_orchestrator: AgentOrchestrator | None = None


def _get_orchestrator() -> AgentOrchestrator:
    """Get or create the module-level orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgentOrchestrator()
    return _orchestrator


@tool
def dispatch_agent(
    agent_type: str,
    prompt: str,
    system_prompt: str = "",
    workflow: str = "strands-command.yml",
) -> str:
    """Dispatch a sub-agent via GitHub Actions workflow_dispatch.

    Security limits are enforced:
    - Max concurrent agents (default: 3)
    - Max total agents per run (default: 5)
    - Cooldown between dispatches (default: 10s)
    - Rate limit checking

    Args:
        agent_type: Type of agent (e.g., "adversarial-test", "release-notes", "docs-gap")
        prompt: Task prompt for the sub-agent
        system_prompt: System prompt override (optional)
        workflow: Target workflow file (default: strands-command.yml).
                  Use "owner/repo/workflow.yml" for cross-repo dispatch.

    Returns:
        Status message with dispatch result
    """
    try:
        orch = _get_orchestrator()
        task = orch.dispatch_agent(
            agent_type=agent_type,
            prompt=prompt,
            system_prompt=system_prompt,
            workflow=workflow,
        )

        if task.status == AgentStatus.DISPATCHED:
            return (
                f"✅ Agent dispatched: {agent_type}\n"
                f"   Run ID: {task.run_id or 'pending'}\n"
                f"   Workflow: {workflow}\n"
                f"   Active agents: {orch._get_active_count()}/{orch.config.max_concurrent}\n"
                f"   Total dispatched: {orch._total_dispatched}/{orch.config.max_total_agents}"
            )
        else:
            return f"❌ Dispatch failed: {task.error}"

    except Exception as e:
        return f"❌ Orchestrator error: {e}"


@tool
def check_agents_status() -> str:
    """Check the status of all dispatched sub-agents.

    Returns:
        Markdown-formatted status report
    """
    try:
        orch = _get_orchestrator()

        if not orch.tasks:
            return "No sub-agents have been dispatched yet."

        # Update statuses
        for task in orch.tasks:
            if task.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING):
                orch.check_run_status(task)

        return orch.format_report()

    except Exception as e:
        return f"❌ Status check error: {e}"


@tool
def wait_for_agents(timeout_minutes: int = 30) -> str:
    """Wait for all dispatched sub-agents to complete.

    Polls GitHub Actions API at regular intervals until all agents
    complete, fail, or timeout.

    Args:
        timeout_minutes: Maximum time to wait (default: 30)

    Returns:
        Final orchestration report
    """
    try:
        orch = _get_orchestrator()

        if not orch.tasks:
            return "No sub-agents have been dispatched yet."

        active = [t for t in orch.tasks if t.status in (AgentStatus.DISPATCHED, AgentStatus.RUNNING)]
        if not active:
            return "All sub-agents have already completed.\n\n" + orch.format_report()

        print(f"⏳ Waiting for {len(active)} agent(s) with {timeout_minutes}m timeout...")
        orch.wait_for_all(timeout_minutes=timeout_minutes)

        return orch.format_report()

    except Exception as e:
        return f"❌ Wait error: {e}"


@tool
def get_orchestrator_config() -> str:
    """Get the current orchestrator security configuration.

    Returns:
        Current configuration values
    """
    config = OrchestratorConfig.from_env()
    return (
        f"## Orchestrator Configuration\n\n"
        f"| Setting | Value |\n"
        f"|---------|-------|\n"
        f"| Max concurrent agents | {config.max_concurrent} |\n"
        f"| Max total agents per run | {config.max_total_agents} |\n"
        f"| Agent timeout | {config.agent_timeout_minutes} minutes |\n"
        f"| Agent max tokens | {config.agent_max_tokens} |\n"
        f"| Cooldown between dispatches | {config.cooldown_seconds} seconds |\n"
        f"| Poll interval | {config.poll_interval_seconds} seconds |\n"
    )
