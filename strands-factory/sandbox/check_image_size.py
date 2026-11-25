#!/usr/bin/env python3
"""Fail when the sandbox image (``Dockerfile.sandbox``) grows past its size budget.

Why a gate: the sandbox image grew from ~260 MB to ~450 MB compressed in three months, and a bigger
image is a slower cold start — 120 s "Runtime initialization time exceeded" wakes killed production
runs. AgentCore's container-image quota is 2 GB, so the budget also keeps a margin below that.

Usage (build the image first)::

    python3 sandbox/check_image_size.py strandly-sandbox:ci

Measures two numbers, both from the layers of ``docker save`` so the result does not depend on the
Docker image store:

* **compressed** — every layer gzipped at the registry's default level (what ECR stores and the
  service pulls). Layers already gzipped in the archive are counted as they are.
* **uncompressed** — every layer unpacked (what the runtime has to unpack). ``docker image inspect``'s
  ``Size`` is not used: with the containerd image store (the default on GitHub's runners) it reports
  the compressed content size, which made this number equal the compressed one and the budget moot.

Stdlib only, so the check needs nothing but Python and Docker. Raise a budget only with a reason in
the commit message; the numbers are the point.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import zlib
from typing import IO

#: Budgets, in MB (10**6 bytes). Production measured ~450 MB compressed / ~1.6 GB uncompressed
#: before the gate; the ceilings leave modest headroom and stay under the 2 GB service quota.
MAX_COMPRESSED_MB = 500
MAX_UNCOMPRESSED_MB = 1800

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_READ_BLOCK = 1 << 20
_INFLATE_SLICE = 1 << 16


def _is_layer(name: str) -> bool:
    """A layer member of a ``docker save`` archive (legacy ``*/layer.tar`` or OCI ``blobs/``)."""
    return name.endswith("layer.tar") or name.startswith("blobs/")


def _layer_sizes(stream: IO[bytes]) -> tuple[int, int]:
    """``(compressed, uncompressed)`` bytes of one layer.

    A gzipped layer is counted as is and inflated for the unpacked size; a raw layer is counted as is
    and gzipped (level 6) for the compressed size.
    """
    first = stream.read(_READ_BLOCK)
    if first[:4] == _ZSTD_MAGIC:
        raise RuntimeError("zstd-compressed layers are not supported; build the image with gzip layers")
    if first[:2] == _GZIP_MAGIC:
        return _gzip_layer_sizes(first, stream)
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    compressed = len(compressor.compress(first))
    uncompressed = len(first)
    while block := stream.read(_READ_BLOCK):
        compressed += len(compressor.compress(block))
        uncompressed += len(block)
    return compressed + len(compressor.flush()), uncompressed


def _gzip_layer_sizes(first: bytes, stream: IO[bytes]) -> tuple[int, int]:
    """Sizes of a gzip stream that starts with ``first``; concatenated gzip members are followed."""
    compressed = uncompressed = 0
    inflater = zlib.decompressobj(31)
    block = first
    while block:
        compressed += len(block)
        # Small slices bound memory: deflate expands at most ~1000x, so ~64 MB per slice.
        for offset in range(0, len(block), _INFLATE_SLICE):
            data = block[offset : offset + _INFLATE_SLICE]
            while data:
                uncompressed += len(inflater.decompress(data))
                if not inflater.eof:
                    break
                data = inflater.unused_data
                if data[:2] != _GZIP_MAGIC and data.strip(b"\0"):
                    raise RuntimeError("unexpected data after the end of a gzipped layer")
                if data[:2] != _GZIP_MAGIC:
                    data = b""  # zero padding
                inflater = zlib.decompressobj(31)
        block = stream.read(_READ_BLOCK)
    return compressed, uncompressed


def layer_sizes(archive: IO[bytes]) -> tuple[int, int]:
    """Total ``(compressed, uncompressed)`` bytes of the layers in a ``docker save`` tar stream.

    OCI archives also keep small JSON blobs (config, manifests) under ``blobs/``; they are counted
    too, which over-counts by kilobytes — immaterial against a budget in hundreds of MB.
    """
    compressed = uncompressed = 0
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            if not member.isfile() or not _is_layer(member.name):
                continue
            extracted = tar.extractfile(member)
            if extracted is not None:
                layer_compressed, layer_uncompressed = _layer_sizes(extracted)
                compressed += layer_compressed
                uncompressed += layer_uncompressed
    return compressed, uncompressed


def violations(
    *,
    compressed: int,
    uncompressed: int,
    max_compressed_mb: float = MAX_COMPRESSED_MB,
    max_uncompressed_mb: float = MAX_UNCOMPRESSED_MB,
) -> list[str]:
    """Human-readable budget violations; empty when the image fits."""
    problems = []
    if compressed > max_compressed_mb * 1e6:
        problems.append(
            f"compressed size {compressed / 1e6:.1f} MB exceeds the {max_compressed_mb:.0f} MB budget"
        )
    if uncompressed > max_uncompressed_mb * 1e6:
        problems.append(
            f"uncompressed size {uncompressed / 1e6:.1f} MB exceeds the "
            f"{max_uncompressed_mb:.0f} MB budget"
        )
    return problems


def measure(image: str) -> tuple[int, int]:
    """``(compressed, uncompressed)`` bytes of a locally loaded image."""
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        sizes = layer_sizes(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"docker save {image} failed with exit code {proc.returncode}")
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="a locally loaded image reference")
    args = parser.parse_args(argv)
    compressed, uncompressed = measure(args.image)
    print(
        f"{args.image}: compressed {compressed / 1e6:.1f} MB (budget {MAX_COMPRESSED_MB} MB), "
        f"uncompressed {uncompressed / 1e6:.1f} MB (budget {MAX_UNCOMPRESSED_MB} MB)"
    )
    problems = violations(compressed=compressed, uncompressed=uncompressed)
    for problem in problems:
        print(f"::error title=sandbox image too large::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
