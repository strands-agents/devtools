#!/usr/bin/env python3
"""Check full license and notice members and isolated installs of the wheel, sdist and rebuilt wheel."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def check_artifact(path: Path, license_bytes: bytes, notice_bytes: bytes) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            metadata_name, = (name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            prefix = metadata_name.removesuffix("METADATA")
            assert archive.read(prefix + "licenses/LICENSE") == license_bytes, path
            assert archive.read(prefix + "licenses/NOTICE") == notice_bytes, path
            metadata = archive.read(metadata_name)
    else:
        with tarfile.open(path) as archive:
            metadata_member, = (member for member in archive.getmembers() if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1)
            prefix = metadata_member.name.removesuffix("PKG-INFO")
            member = archive.extractfile(prefix + "LICENSE")
            assert member is not None and member.read() == license_bytes, path
            member = archive.extractfile(prefix + "NOTICE")
            assert member is not None and member.read() == notice_bytes, path
            member = archive.extractfile(metadata_member)
            assert member is not None
            metadata = member.read()
    parsed = BytesParser().parsebytes(metadata)
    assert parsed["License-Expression"] == "Apache-2.0", path
    assert parsed.get_all("License-File") == ["LICENSE", "NOTICE"], path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--canonical-license", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    out = args.directory.resolve()
    out.mkdir(parents=True, exist_ok=False)
    license_bytes = args.canonical_license.read_bytes()
    license_hash = hashlib.sha256(license_bytes).hexdigest()
    notice_bytes = args.canonical_license.with_name("NOTICE").read_bytes()
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "STRANDLY_")) and k not in {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_MAINTAINER_TOKEN", "PYTHONPATH", "VIRTUAL_ENV"}}
    for variable, arguments in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"])):
        environment[variable] = subprocess.check_output(["uv", *arguments], text=True).strip()
    home = out / "home"
    home.mkdir()
    environment.update(HOME=str(home), AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull, AWS_EC2_METADATA_DISABLED="true")

    def run(command: list[str], cwd: Path = out) -> None:
        print("RUN", " ".join(command), flush=True)
        subprocess.run(command, cwd=cwd, env=environment, check=True)

    wheel, = args.dist.resolve().glob("strandly_mcp-*.whl")
    sdist, = args.dist.resolve().glob("strandly_mcp-*.tar.gz")
    for artifact in (wheel, sdist):
        check_artifact(artifact, license_bytes, notice_bytes)
    source = out / "source"
    with tarfile.open(sdist) as archive:
        # Python 3.10 also runs this gate; validate regular members before extraction.
        for member in archive.getmembers():
            assert not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            assert member.isfile() or member.isdir()
        archive.extractall(source)  # noqa: S202
    extracted, = source.iterdir()
    run(["uv", "build", "--wheel", str(extracted), "--out-dir", str(out / "rebuilt")])
    rebuilt, = (out / "rebuilt").glob("*.whl")
    check_artifact(rebuilt, license_bytes, notice_bytes)
    result: dict = {"license_sha256": license_hash, "artifacts": {}}
    for label, artifact in (("wheel", wheel), ("sdist", sdist), ("rebuilt_wheel", rebuilt)):
        consumer = out / label
        consumer.mkdir()
        venv = consumer / ".venv"
        python = venv / "bin/python"
        run(["uv", "venv", "--python", sys.executable, str(venv)], consumer)
        run(["uv", "pip", "install", "--python", str(python), str(artifact)], consumer)
        run([str(python), "-I", "-c", """
import hashlib, importlib.util, sys
from importlib.metadata import distribution
from pathlib import Path
import strandly_mcp, strandly_mcp.auth, strandly_mcp.cli, strandly_mcp.dashboard, strandly_mcp.server, strandly_mcp.sessions
assert Path(strandly_mcp.__file__).is_relative_to(Path(sys.prefix))
for name in ('strandly', 'strands', 'strands_harness', 'strands_tools', 'boto3'):
    assert importlib.util.find_spec(name) is None, name
    assert not any(m.split('.')[0] == name for m in sys.modules), name
package = distribution('strandly-mcp')
assert package.metadata['License-Expression'] == 'Apache-2.0'
assert package.metadata.get_all('License-File') == ['LICENSE', 'NOTICE']
for name, digest in (('LICENSE', sys.argv[1]), ('NOTICE', sys.argv[2])):
    member, = (p for p in package.files if str(p).endswith('.dist-info/licenses/' + name))
    assert hashlib.sha256(package.locate_file(member).read_bytes()).hexdigest() == digest, name
print('isolated installed license and thin-client imports: PASS')
""", license_hash, hashlib.sha256(notice_bytes).hexdigest()], consumer)
        run([str(venv / "bin/strandly-mcp"), "--help"], consumer)
        result["artifacts"][label] = {"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "license_member_equal": True, "notice_member_equal": True, "isolated_install": True}
    (out / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
