"""Security + session invariants for the deploy/invoke workflows that actionlint can't see.

Everything structural (YAML/shell correctness) is covered by `actionlint` in CI. These only guard
the non-obvious *negative space* — bits a careless edit silently breaks:

1. the invoke workflow can never touch the privileged deploy role (blast-radius split),
2. a user-supplied prompt is never inlined into a shell `run:` (script-injection), and
3. the invoke session id is *derived from the GitHub context*, never a hard-coded ephemeral default
   (the #352 regression: a pinned ``SESSION_ID=ci-<run_id>`` short-circuited the canonical scheme,
   so an Action invoke never threaded into ``gh-…-pr-N`` with the rest of the ingresses).
"""

from pathlib import Path

import yaml

# The package lives in <repo>/strandly-harness/; the workflows live at the devtools repo root.
_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"
DEPLOY = (_WF / "strandly-deploy.yml").read_text()
INVOKE = (_WF / "strandly-invoke.yml").read_text()


def test_invoke_role_is_not_the_deploy_role():
    # The whole point of the OIDC split: an invoke run can't escalate to redeploying the agent.
    assert "AWS_INVOKE_ROLE_ARN" in INVOKE and "AWS_DEPLOY_ROLE_ARN" not in INVOKE
    assert "AWS_DEPLOY_ROLE_ARN" in DEPLOY and "AWS_INVOKE_ROLE_ARN" not in DEPLOY


def test_prompt_reaches_the_shell_via_env_not_inlined():
    # A crafted prompt inlined as `${{ inputs.prompt }}` in a run block could run arbitrary commands
    # on the runner and exfiltrate the OIDC creds. It must travel through env (referenced as $PROMPT).
    for step in yaml.safe_load(INVOKE)["jobs"]["invoke"]["steps"]:
        run = step.get("run", "")
        assert "inputs.prompt" not in run
        # The mention ingress bodies are user-controlled text too — same rule.
        assert "event.comment.body" not in run and "event.issue.body" not in run


def test_invoke_session_is_derived_from_github_context_not_a_hardcoded_default():
    # Regression guard for #352: invoke.yml used to pin `SESSION_ID=ci-<run_id>`, and the resolver
    # honors SESSION_ID first — so the canonical `gh-<owner>-<repo>-<kind>-<number>` derivation never
    # ran and an Action invoke landed in its own ephemeral session instead of the item's thread.
    job = yaml.safe_load(INVOKE)["jobs"]["invoke"]
    job_env = job.get("env", {})
    # SESSION_ID may exist only as an *optional explicit override* — never a baked-in ephemeral id.
    session_default = str(job_env.get("SESSION_ID", ""))
    assert "run_id" not in session_default and "ci-" not in session_default
    # The derivation needs the GitHub context, resolved via the one shared helper every ingress uses.
    assert "GITHUB_CONTEXT" in job_env
    assert any(
        "session_id_from_github_event" in step.get("run", "") for step in job["steps"]
    ), "invoke.yml must resolve its session id via strandly_harness.ops.lambdas.mention_poller.sessions"


def test_mention_ingress_is_gated_on_maintainer_association():
    # The event triggers (issues/issue_comment) MUST be gated: body contains @strandly AND the
    # author is OWNER/MEMBER/COLLABORATOR and not a bot. Otherwise any drive-by account could
    # spend invocations / steer the deployed agent.
    wf = yaml.safe_load(INVOKE)
    triggers = wf.get("on", wf.get(True, {}))  # yaml 1.1 parses bare `on:` as boolean True
    assert "issues" in triggers and "issue_comment" in triggers
    gate = wf["jobs"]["invoke"].get("if", "")
    assert "@strandly" in gate
    assert "author_association" in gate
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert assoc in gate
    assert "Bot" in gate
