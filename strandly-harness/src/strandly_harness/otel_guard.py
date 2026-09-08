"""OpenTelemetry environment sanitation.

AWS runtimes (and AgentCore) often ship ``OTEL_PROPAGATORS=xray`` in the environment. OpenTelemetry
resolves that value to a propagator **entry point at import time**; if the AWS X-Ray propagator
package isn't installed, the import raises and takes the whole process down before the agent ever
runs. Strands' own telemetry sets the W3C ``tracecontext``/``baggage`` propagators internally, so
the env value buys us nothing — it only risks that crash.

:func:`sanitize_otel_env` rewrites a stray ``xray`` out of ``OTEL_PROPAGATORS`` (keeping any other
listed propagators, defaulting to ``tracecontext,baggage`` if that empties it). It must run **before
the first ``strands``/``opentelemetry`` import**, so it is called at the top of the package
``__init__`` and in the test ``conftest`` — which is why nobody needs to ``export`` it by hand.
"""

from __future__ import annotations

import os

# Propagator names that need the optional AWS X-Ray package; we drop them to avoid the import crash.
_XRAY_NAMES = {"xray", "aws_xray", "awsxray"}
_DEFAULT = "tracecontext,baggage"


def sanitize_otel_env(env: dict[str, str] | None = None) -> None:
    """Strip an X-Ray propagator from ``OTEL_PROPAGATORS`` in place (defaults to ``os.environ``).

    No-op when the variable is unset or contains no X-Ray entry. When removing X-Ray empties the
    list, falls back to ``tracecontext,baggage`` so propagation still works.
    """
    target = os.environ if env is None else env
    raw = target.get("OTEL_PROPAGATORS")
    if not raw:
        return
    kept = [p.strip() for p in raw.split(",") if p.strip() and p.strip().lower() not in _XRAY_NAMES]
    new_value = ",".join(kept) if kept else _DEFAULT
    if new_value != raw:
        target["OTEL_PROPAGATORS"] = new_value
