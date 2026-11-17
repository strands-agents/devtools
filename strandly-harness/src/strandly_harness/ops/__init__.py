"""Ops: the AWS-side operational plane — everything that *drives or watches* a deployed Strandly.

Contract: **nothing in ``ops/`` — or the strands-free ``core`` modules it leans on
(``core.config``/``core.constants``/``core.context``/``core.session_ids``) — may import the Strands
SDK or any agent-runtime dependency**. These modules run in Lambda bundles and CI where the SDK
isn't installed (enforced by ``tests/unit/ops/test_import_hygiene.py``, which walks ``ops.*`` plus
those cross-boundary core modules).

- ``runtime_client`` — fire-and-forget ``InvokeAgentRuntime`` client (boto3 only).
- ``ledger`` — DynamoDB run ledger (dispatch/completion records).
- ``metrics`` — CloudWatch EMF metric emission (stdlib only).
- ``lambdas/`` — deployed Lambda handlers: ``mention_poller/`` (GitHub @mention poller + audit +
  dedup), ``scheduled/`` (cron self-invocations), ``stuck_runs`` (stuck-run detector).
"""
