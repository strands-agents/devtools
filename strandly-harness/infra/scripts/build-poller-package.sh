#!/usr/bin/env bash
# Build the mention-poller Lambda deployment package.
#
# The poller imports only `strandly_harness.ops.*` (+ the import-light `core.config`/`core.constants`
# and the top-level package init) — the ops/ subtree is a machine-enforced strands-free zone
# (see tests/unit/ops/test_import_hygiene.py), and boto3 comes from the Lambda runtime. So the
# bundle is the *package source only* (`pip install . --no-deps`): no dependency resolution, no
# Strands SDK, kilobytes instead of the ~230 MB the pre-refactor bundle weighed.
#
# Run from the repo root (it does `pip install .`).
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

build_package() {
  local target="$1"
  echo ">> Installing strandly_harness (source only, --no-deps) into ${target}…"
  rm -rf "${target}"
  mkdir -p "${target}"
  pip install . --target "${target}" --no-deps --upgrade
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
