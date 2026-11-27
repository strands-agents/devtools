#!/usr/bin/env bash
# Build the mention-poller Lambda deployment package.
#
# The poller imports only `strandly.ops.*` (+ the import-light `core.config`/`core.constants`
# and the top-level package init) — the ops/ subtree is a machine-enforced strands-free zone
# (see tests/harness/ops/test_import_hygiene.py), and boto3 comes from the Lambda runtime. So the
# bundle is the *package source only* (`uv pip install . --no-deps`): no dependency resolution, no
# Strands SDK, kilobytes instead of the ~230 MB the pre-refactor bundle weighed. The one addition
# is `tzdata` (pure data): the scheduled invoker resolves job timezones with `zoneinfo`, and the
# python3.12 Lambda runtime (Amazon Linux 2023, minimal) is not guaranteed to ship
# /usr/share/zoneinfo; `zoneinfo` falls back to the `tzdata` package.
#
# Run from the source/materialized workspace root; needs Python and uv, not pip.
#
# Two output modes:
#
#   # Local asset dir — what the CDK IngressStack references via Code.from_asset (default path
#   # infra/build/poller). This is the path for `cdk deploy`.
#   infra/scripts/build-poller-package.sh --local infra/build/poller
#
#   # S3 zip — legacy, for a hand-rolled Lambda update outside CDK.
#   infra/scripts/build-poller-package.sh <s3-bucket> [s3-key]
set -euo pipefail

TZDATA_VERSION="2026.6"

build_package() {
  local target="$1"
  echo ">> Installing strandly (source only, --no-deps) into ${target}…"
  rm -rf "${target}"
  mkdir -p "${target}"
  uv pip install --python "${PYTHON:-python3}" . --target "${target}" --no-deps --upgrade
  if [[ -n "${STRANDLY_BUILD_ADDON_SOURCE:-}" ]]; then
    test -d "$STRANDLY_BUILD_ADDON_SOURCE" || { echo "Configured addon source missing" >&2; return 1; }
    cp -R "$STRANDLY_BUILD_ADDON_SOURCE"/. "$target"/
  fi
  uv pip install --python "${PYTHON:-python3}" "tzdata==${TZDATA_VERSION}" --target "${target}" --no-deps --upgrade
  "${PYTHON:-python3}" - "${target}" <<'PYTHON'
import runpy
import sys
from pathlib import Path
helpers = runpy.run_path("infra/scripts/build_agent_bundle.py")
target = Path(sys.argv[1])
helpers["strip_local_install_metadata"](target)
helpers["fix_shebangs"](target / "bin")
PYTHON
}

if [[ "${1:-}" == "--local" ]]; then
  DEST="${2:?usage: build-poller-package.sh --local <asset-dir>}"
  build_package "${DEST}"
  echo ">> Done. Asset built at ${DEST}."
  echo "   Deploy with: cd infra && cdk deploy '*-Ingress-*' -c poller_asset=$(cd "${DEST}" && pwd)"
  exit 0
fi

BUCKET="${1:?usage: build-poller-package.sh --local <dir>  |  <s3-bucket> [s3-key]}"
KEY="${2:-strandly-mention-poller.zip}"
BUILD_DIR="$(mktemp -d)"
ZIP_PATH="${BUILD_DIR}/package.zip"

build_package "${BUILD_DIR}/package"

echo ">> Zipping…"
(cd "${BUILD_DIR}/package" && zip -qr "${ZIP_PATH}" . -x '*.pyc' -x '*/__pycache__/*')

echo ">> Uploading to s3://${BUCKET}/${KEY}…"
aws s3 cp "${ZIP_PATH}" "s3://${BUCKET}/${KEY}"

echo ">> Done. (Legacy S3 mode — CDK uses --local instead.)"
