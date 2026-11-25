# Goals: e2e-test

- Tests actually ran against live Bedrock: clone, env (`AWS_REGION`, `GITHUB_ACTIONS` unset), and
  `tests_integ` execution with real output in the transcript — not described.
- Live model calls went through Bedrock; any direct Anthropic/OpenAI run states why the test needed
  that provider rather than Bedrock.
- Every failure has its traceback and a call: SDK regression vs environment/skip artifact.
- Boundary respected: created resources tagged `ManagedBy=<test-tag>` / named `<test-prefix>-*`;
  no touch of resources carrying the deployment's protected tag; no IAM role creation.
- Created resources deleted, or leftovers listed by id.
- A blocked run is reported plainly with evidence, not papered over as a pass.
