---
name: e2e-test
description: >
  Run Strands Agents END-TO-END / integration tests against LIVE Bedrock from inside the sandbox,
  using the scoped AWS credentials the sandbox carries when it's deployed with a CI Bedrock role.
  TRIGGER when asked to "run the integ tests", "test strands e2e", "exercise the SDK against live
  Bedrock", or to validate a change against real model invocations. SKIP for unit tests (no AWS
  needed — just run pytest) or when the sandbox has no AWS credentials. Hard rule: every AWS
  resource you create MUST be tagged ManagedBy=strandly and named strandly-managed-* — the IAM role
  enforces it, and it's what stops you from touching production.
compatibility: >
  Requires the sandbox deployed with the CI Bedrock role (-c ci_bedrock_role=true) so scoped AWS
  credentials are present; without them, run unit tests instead.
allowed-tools: bash file_editor use_github think
---

# E2E testing Strands against live Bedrock

You're running Strands Agents' real integration tests — the ones that invoke **live Bedrock** (and
the OpenAI-compatible **Bedrock Mantle** endpoint) — from inside your sandbox. Unit tests don't need
this skill; this is specifically for the `tests_integ` suite that calls real models.

## Your credentials and their hard boundary — read this first

When the sandbox is deployed with the CI Bedrock role (`-c ci_bedrock_role=true`), your environment
has **scoped** AWS credentials (via the sandbox's instance metadata — boto3/`aws` pick them up
automatically; no setup). They let you:

- **Invoke any Bedrock model** (`InvokeModel`, `Converse`, streaming, `CountTokens`, `ApplyGuardrail`)
  and call **Bedrock Mantle**. This is invoke-only — you cannot manage models.
- **Create and operate test resources** (Knowledge Bases, guardrails, data sources, S3 buckets) —
  but **only** under a strict tag/name boundary.

**The boundary (the IAM role enforces this — it is not advisory):**

1. **Every resource you create MUST be tagged `ManagedBy=strandly`** at creation time. A create call
   without that tag gets **AccessDenied**. (For Bedrock: pass `tags={"ManagedBy": "strandly"}` to
   `create_knowledge_base` / `create_guardrail` / etc. For S3: tag the bucket on create.)
2. **You can only read/update/delete resources already tagged `ManagedBy=strandly`.** Anything else
   is invisible to you.
3. **S3 buckets MUST be named `strandly-managed-<something>`** — other names get AccessDenied on
   create, and you can only touch `strandly-managed-*` buckets.
4. **Production is off-limits and unreachable.** Strandly's own KB, Memory, runtime, and config are
   tagged `ManagedBy=strandly-infra` (a *different* value), so your grants literally cannot touch
   them. Do not attempt to — you'll get AccessDenied, and trying is a red flag. You cannot modify
   yourself.
5. **You cannot create IAM roles.** A Knowledge Base needs a service role; a pre-made one exists
   (`*_managed_kb_role`) — pass its ARN to `create_knowledge_base`. Do not try to mint roles.

If a call you expected to work returns AccessDenied, check the tag/name first — the boundary is
almost always the cause, and it's working as designed. Don't try to route around it.

## Running the tests

1. **Get the SDK.** Clone the repo into your sandbox (you have public internet and `git` — the
   harness bootstraps a real git into your sandbox on session start, so `git clone`/`commit`/`push`
   all work): `git clone https://github.com/strands-agents/harness-sdk /tmp/sdk` (the Python SDK is
   under `strands-py/`). Check out the branch/PR under test if you're validating a change.
   - If `git` is somehow missing (a rare bootstrap failure — you'll know because `git --version`
     fails), you can still fetch source read-only via the GitHub archive tarball
     (`curl -fsSL https://codeload.github.com/strands-agents/harness-sdk/tar.gz/<ref> | tar xz`),
     but note that path can't `push` — prefer git.
2. **Set up the environment:**
   - `export AWS_REGION=us-west-2` (the tests default to a region; match the sandbox's). A few tests
     pin `us-west-2` for S3-media; the rest follow `AWS_REGION`.
   - **Leave `GITHUB_ACTIONS` unset.** In CI mode the suite *requires* third-party provider API keys
     (OpenAI/Anthropic-direct/etc.) and errors without them; unset, those non-Bedrock provider tests
     simply **skip**, which is what you want — you're testing the Bedrock path.
   - Install: `cd /tmp/sdk/strands-py && pip install -e '.[dev]'` (or use `hatch`). `pytest` is not
     preinstalled — `pip install pytest` if `.[dev]` doesn't pull it. If a source *tarball* (not a
     git clone) fails to install with a hatch-vcs/"version from git tags" error, export
     `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0` before installing; a real git clone has tags and doesn't
     hit this.
3. **Run the Bedrock-touching integ tests**, e.g.:
   - `hatch test tests_integ` (the directory is the selector — there's no `@pytest.mark.integ`), or
   - target the model files directly: `pytest tests_integ/models/test_model_bedrock.py
     tests_integ/models/test_model_mantle.py -v`.
   - KB / guardrail / S3-media tests need provisioned infra or will skip — that's expected; focus on
     what runs.
4. **Read the results like a reviewer.** Don't just report "N passed/failed" — for any failure, pull
   the actual assertion/traceback, decide whether it's a real SDK regression vs. an environment/skip
   issue (missing creds for a non-Bedrock provider, an un-provisioned KB), and say which.

## Clean up after yourself

Anything you created (a `ManagedBy=strandly` KB, a `strandly-managed-*` bucket, a test guardrail)
**you delete** when the test run is done — these cost money and accumulate. Track what you create and
tear it down in a `finally`-style pass even if the tests fail. If you can't delete something, say so
explicitly with its id so a human can.

## Reporting

Lead with the verdict (did the SDK pass against live Bedrock?), then the evidence: the exact command
you ran, the pass/fail/skip counts, and for each real failure the grounded detail (file:line +
traceback + your read on whether it's an SDK bug). Note anything you created and confirm you cleaned
it up. If the run was blocked (no creds, region mismatch, a boundary AccessDenied you couldn't
resolve), report that honestly rather than claiming a pass.

**Be precise and honest about what you actually did — especially in a public review.** Never claim a
tool or dependency "isn't available/installable" until you've *tried* (clone, `pip install`, the
tarball fallback, `pip install pytest`). If you genuinely couldn't run the tests, say **exactly**
what stopped you (e.g. "the install failed with `<error>`", "the sandbox has no network to PyPI"),
not a guessed cause. "I could not execute the suite; findings are static (file:line)" is a fine,
honest thing to write — a wrong root cause posted publicly is not. If you only did static analysis,
say so plainly and don't imply you ran anything.
