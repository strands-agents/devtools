"""The operational plane: Lambdas and helpers that drive or watch a deployed Strandly.

Nothing in ``ops`` (or the strands-free ``core`` modules it imports) may import the Strands SDK or
another agent-runtime dependency: these modules run in Lambda bundles without them
(``tests/harness/ops/test_import_hygiene.py``).
"""
