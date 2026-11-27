---
name: e2e-test
description: >
  Run Strands Agents END-TO-END / integration tests against LIVE models from inside the sandbox:
  Bedrock by default, plus keyless direct Anthropic/OpenAI/Vertex AI, opt-in SageMaker endpoints,
  and local Ollama/llama.cpp when a model provider itself is under test. Every AWS
  resource you create MUST be tagged ManagedBy=<test-tag> and named <test-prefix>-* — the IAM role
  enforces it. TRIGGER on "run the integ tests", "test strands e2e", or validating a change against
  real model invocations, including a specific provider adapter. SKIP for unit tests (just run pytest) and when the sandbox has no AWS
  credentials.
compatibility: >
  Requires the sandbox deployed with Bedrock grants (-c sandbox_bedrock_grants=true, the default)
  so scoped AWS credentials are present; without them, run unit tests instead.
allowed-tools: shell read write edit use_github
---

# E2E testing against live Bedrock

## Deployment scope (required)

Read the **AWS test-resource scope** supplied in the agent instructions. In the examples below,
`<test-tag>`, `<test-prefix>`, and `<protected-tag>` mean those exact deployment-provided values;
substitute them before running commands. If the scope is absent, obtain explicit operator inputs
before creating cloud resources. Never derive a tag, account or resource prefix from a GitHub
login or assume the product's legacy scope. Scope hints do not themselves authorize AWS calls.

## The credential boundary (IAM-enforced, not advisory)

The sandbox's scoped credentials (instance metadata; boto3/`aws` pick them up) allow Bedrock
invocation (`InvokeModel`, `Converse`, streaming, `CountTokens`, `ApplyGuardrail`, Mantle) and test
resources (KBs, guardrails, data sources, S3) **only** under these rules:

1. Every resource you create is tagged `ManagedBy=<test-tag>` at creation (Bedrock:
   `tags={"ManagedBy": "<test-tag>"}`; S3: tag on create) — otherwise AccessDenied.
2. You can only read/update/delete resources carrying that tag.
3. S3 buckets are named `<test-prefix>-*`.
4. Production (`ManagedBy=<protected-tag>`) is unreachable by design. Don't try.
5. No IAM role creation — a KB uses the pre-made `*_managed_kb_role`.

An unexpected AccessDenied almost always means the tag/name; don't route around it.

## Run

1. `git clone --filter=blob:none https://github.com/strands-agents/harness-sdk /mnt/workspace/sdk` (Python SDK under
   `strands-py/`; the workspace persists, `/tmp` is wiped on a sandbox restart); check out the branch/PR under test. (`git` missing → tarball from
   `codeload.github.com/…/tar.gz/<ref>`, read-only.)
2. `export AWS_REGION=us-west-2`; leave `GITHUB_ACTIONS` unset (set, the suite *requires*
   third-party provider keys; unset, those tests skip). `cd strands-py && pip install -e '.[dev]'`
   (`pip install pytest` if missing; tarball install failing on hatch-vcs →
   `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0`).
3. `hatch test tests_integ`, or target `pytest tests_integ/models/test_model_bedrock.py
   tests_integ/models/test_model_mantle.py -v`. KB / guardrail / S3-media tests skip without
   provisioned infra — expected.
4. Read failures like a reviewer: pull the traceback and say whether it's an SDK regression or an
   environment/skip artifact.

## Bedrock first; direct providers only when the provider is the subject

Run every live model call through Bedrock, including Claude and OpenAI models Bedrock hosts, and
including `LiteLLMModel` (`bedrock/...`) and `VercelModel` (`@ai-sdk/amazon-bedrock`), which use the
sandbox role as-is. Use direct Anthropic, OpenAI, or Google Vertex AI only when the change or bug is
specific to that provider's API or adapter (`AnthropicModel`, `OpenAIModel`, `GeminiModel`/
`GoogleModel`, or LiteLLM/Vercel routing to it), or to verify an anomaly seen only on models hosted
there (e.g. it reproduces on api.anthropic.com but not on Bedrock). Gemini is not on Bedrock, so
Gemini tests always go direct. Say which case applies in the report; if Bedrock can answer the
question, a direct-provider run is out of scope.

Direct federation is configured when the sandbox has `ANTHROPIC_FEDERATION_RULE_ID`,
`OPENAI_IDENTITY_PROVIDER_ID`, or `GOOGLE_WIF_POOL_ID`. Python: import
`/opt/strandly/strandly_sandbox_auth.py` (on `PYTHONPATH`) and pass `anthropic_client_args()`,
`openai_client_args()`, or `gemini_client_args()` as the SDK constructor kwargs or Strands
`client_args`; for `LiteLLMModel` use `litellm_anthropic_args()`, `litellm_openai_args()`, or
`litellm_vertex_args()`. TypeScript: copy `/opt/strandly/strandly_sandbox_auth.mjs` into the project
under test (never commit it) and build a `client` (or Vercel provider) from its exports. Install the
SDK versions in `docs/operations.md` (Direct model providers); SDKs refresh tokens themselves.

Do not mint a token once and export it as an API key. Never print tokens or put them in shell
arguments, `shell(env=...)`, or workspace files: command bodies are logged. API-key-only fixtures
(including the Gemini AI Studio ones) still need adapting; absent federation configuration means
skip those tests, not search for keys. Use only the configured workspace/project and report model
usage; short TTLs are not spending caps.

## SageMaker (`SageMakerAIModel`)

Only when the SageMaker provider is under test and `aws sagemaker list-endpoints` works (opt-in).
Name the model, endpoint config, and endpoint `<test-prefix>-<id>` and tag each
`ManagedBy=<test-tag>` at creation. Model: image `763104351884.dkr.ecr.<region>.amazonaws.com/djl-inference:0.38.0-lmi30.0.0-cu130`,
execution role `*_sandbox_managed_sagemaker_role`, env `HF_MODEL_ID=Qwen/Qwen2.5-7B-Instruct`,
`OPTION_MAX_MODEL_LEN=8192`, `OPTION_ENABLE_AUTO_TOOL_CHOICE=true`, `OPTION_TOOL_CALL_PARSER=hermes`.
Config: one `ml.g5.xlarge` instance (`ml.g5.2xlarge` if it fails to load),
`InferenceAmiVersion=al2023-ami-sagemaker-inference-gpu-4-1` (CUDA 13),
`ContainerStartupHealthCheckTimeoutInSeconds=900`. The integ test hardcodes `us-east-1`; create
there. Wait for `InService`, then `SAGEMAKER_ENDPOINT_NAME=<name> pytest tests_integ/models/test_model_sagemaker.py`.
It bills hourly: in a `finally`, ALWAYS delete endpoint, endpoint config, and model, even on
failure, then `aws sagemaker list-endpoints --name-contains <test-prefix>-` and report leftovers.

## Ollama / llama.cpp

Local servers, no AWS. `/opt/strandly/local-llm` starts them, runs
your command, and stops them, so make it ONE `shell` call with `timeout=1800` (the tool defaults
to 120s). From `strands-py/` after `pip install -e '.[all,dev]'`:

```bash
/opt/strandly/local-llm --alias llama3.3:70b --llama-server -- env LLAMACPP_SKIP_TESTS=false \
  LLAMACPP_TEST_URL=http://127.0.0.1:8081 python -m pytest -o asyncio_mode=auto -v \
  tests_integ/models/test_model_ollama.py tests_integ/models/test_model_llamacpp.py
```

`--alias` serves `qwen2.5:3b` (`PARAMETER num_thread 2`) as the `llama3.3:70b` the suite hard-codes.
`LLAMACPP_TEST_URL` has no `/v1`: the adapter appends it. Test the current `main` checkout:
`test_streaming_chunks` fails on SDKs before 1.54.0 (missing usage chunk, fixed in #3955).

## Clean up and report

Delete everything you created (track it; tear down even if tests fail; list any leftover ids).
Report verdict first, then the exact command, pass/fail/skip counts, and per-failure `file:line` +
traceback + your call. If blocked (no creds, AccessDenied, install failure), say exactly what
stopped you — never claim a pass, never guess a cause, never imply you ran what you only read, and
never call something unavailable you didn't actually try to install or run.
