"""Local install provenance must not escape into deployment artifacts."""
import csv
import runpy
from pathlib import Path


def test_local_install_metadata_is_removed_and_record_remains_consistent(tmp_path):
    metadata = tmp_path / "example-0.1.dist-info"
    metadata.mkdir()
    for name in ("direct_url.json", "uv_cache.json"):
        (metadata / name).write_text('{"origin":"local-build-source"}')
    (metadata / "METADATA").write_text("Name: example\nVersion: 0.1\n")
    record = metadata / "RECORD"
    with record.open("w", newline="") as stream:
        csv.writer(stream).writerows([[f"{metadata.name}/{name}", "", ""] for name in ("direct_url.json", "uv_cache.json", "METADATA")])
    module = runpy.run_path(str(Path(__file__).resolve().parents[3] / "infra/scripts/build_agent_bundle.py"))
    module["strip_local_install_metadata"](tmp_path)
    assert not (metadata / "direct_url.json").exists()
    assert not (metadata / "uv_cache.json").exists()
    assert (metadata / "METADATA").read_text() == "Name: example\nVersion: 0.1\n"
    with record.open(newline="") as stream:
        assert list(csv.reader(stream)) == [[f"{metadata.name}/METADATA", "", ""]]
