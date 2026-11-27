"""Every shipped module must compile without a ``SyntaxWarning``.

A ``SyntaxWarning: invalid escape sequence`` is emitted by CPython at **compile** time, i.e. on the
first import of the module — which means once per CI job, once per Lambda cold start, and once per
AgentCore instance start, in a log nobody wants noise in. ``pyproject.toml``'s ``filterwarnings``
only ignores ``DeprecationWarning``, so these are not even suppressed.

They are also a genuine (if small) correctness smell: the offending literal does not contain what
its author wrote. ``\\`` in a non-raw string is preserved *today* only because ``\\`` is not a
recognized escape — CPython has warned since 3.6 and the plan of record is to make it an error.

This guard compiles every ``src/`` module with warnings promoted to errors, so the next one fails
here instead of in production logs.
"""

from __future__ import annotations

import py_compile
import tempfile
import warnings
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "harness" / "src"


def test_no_module_emits_a_syntax_warning_when_compiled():
    modules = sorted(_SRC.rglob("*.py"))
    assert modules, f"no modules found under {_SRC} — the glob is wrong, not the code"

    offenders: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        out = str(Path(tmp) / "out.pyc")
        for path in modules:
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                try:
                    py_compile.compile(str(path), cfile=out, doraise=True)
                except py_compile.PyCompileError as exc:
                    offenders.append(f"{path.relative_to(_SRC)}: {exc.msg.strip()}")

    assert not offenders, "SyntaxWarning at import time (use a raw string):\n" + "\n".join(offenders)
