"""The sandbox image size gate (``sandbox/check_image_size.py``) and its CI wiring."""

from __future__ import annotations

import gzip
import importlib.util
import inspect
import io
import os
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_sandbox_image_size", ROOT / "sandbox" / "check_image_size.py"
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _archive(members: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def test_raw_layers_are_gzipped_for_the_compressed_size_and_metadata_is_ignored():
    raw = os.urandom(200_000)  # incompressible: gzip ≈ the input
    zeros = b"\0" * 1_000_000  # very compressible
    archive = _archive({"abc/layer.tar": raw, "def/layer.tar": zeros, "manifest.json": b"{}" * 1000})
    compressed, uncompressed = gate.layer_sizes(archive)
    assert 200_000 < compressed < 200_000 + 10_000
    assert uncompressed == 1_200_000


def test_already_gzipped_oci_blobs_are_counted_as_they_are_and_inflated_for_the_unpacked_size():
    """The containerd image store saves gzipped layers; ``docker image inspect`` then reports the
    compressed size, so the unpacked size has to come from inflating the layers."""
    content = os.urandom(50_000) + b"\0" * 3_000_000
    blob = gzip.compress(content)
    archive = _archive({"blobs/sha256/aaa": blob, "index.json": b"{}"})
    assert gate.layer_sizes(archive) == (len(blob), len(content))


def test_a_layer_of_concatenated_gzip_members_is_inflated_to_the_end():
    blob = gzip.compress(b"a" * 70_000) + gzip.compress(b"b" * 130_000)
    archive = _archive({"blobs/sha256/bbb": blob})
    assert gate.layer_sizes(archive) == (len(blob), 200_000)


def test_zstd_layers_are_refused_rather_than_miscounted():
    archive = _archive({"blobs/sha256/ccc": b"\x28\xb5\x2f\xfd" + b"\0" * 100})
    with pytest.raises(RuntimeError, match="zstd"):
        gate.layer_sizes(archive)


def test_measure_does_not_trust_docker_inspect_for_the_unpacked_size():
    assert "inspect" not in inspect.getsource(gate.measure)


def test_budget_violations():
    assert gate.violations(compressed=400_000_000, uncompressed=1_500_000_000) == []
    problems = gate.violations(compressed=600_000_000, uncompressed=1_900_000_000)
    assert len(problems) == 2
    assert "compressed size 600.0 MB" in problems[0]


def test_the_budget_stays_under_the_service_image_quota():
    """AgentCore's container-image quota is 2 GB; the gate must leave a margin below it."""
    assert gate.MAX_UNCOMPRESSED_MB < 2000
    assert gate.MAX_COMPRESSED_MB < gate.MAX_UNCOMPRESSED_MB


