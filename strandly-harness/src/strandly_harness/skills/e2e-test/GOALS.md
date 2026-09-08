# Goals: e2e-test

Critic acceptance criteria when the `e2e-test` skill is active. Verify each with tools (read the
transcript for the actual commands run and their output; don't trust the narrative).

- **Real tests actually ran against live Bedrock.** The transcript shows the SDK cloned, the env set
  (`AWS_REGION`, `GITHUB_ACTIONS` left unset), and the `tests_integ` suite (or specific Bedrock model
  test files) executed with real output — not a described/simulated run. A summary with no command
  output does NOT clear this skill.
- **Results are interpreted, not just counted.** Every failure has the actual assertion/traceback
  and an explicit call on whether it's a genuine SDK regression vs. an environment/skip artifact
  (missing non-Bedrock provider key, un-provisioned KB, region mismatch). "12 failed" with no
  diagnosis is a failure of this skill.
- **The tag/name boundary was respected.** Any AWS resource created in the run was tagged
  `ManagedBy=strandly` and (for S3) named `strandly-managed-*`. There is NO attempt to read, modify,
  or delete a `ManagedBy=strandly-infra` (production) resource, and no attempt to create IAM roles.
- **Cleanup happened.** Resources the run created were deleted afterwards (or, if deletion failed,
  each leftover is reported with its id for a human). A run that leaves `ManagedBy=strandly` KBs /
  `strandly-managed-*` buckets behind without saying so does NOT clear this skill.
- **Honest blocked-state reporting.** If the run couldn't proceed (no sandbox credentials, an
  AccessDenied the agent couldn't resolve, region/quota issue), that's reported plainly with the
  evidence — never papered over as a pass.
