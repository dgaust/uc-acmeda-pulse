#!/usr/bin/env bash
#
# Build the install file for the Remote: uc-intg-acmeda-<version>-aarch64.tar.gz
#
# Uses the official Unfolded Circle PyInstaller builder image to produce an
# ARM64 binary, then packs it in the archive layout the Remote expects
# (driver.json in the root, the binary at bin/driver).
#
# Needs docker. On an x86 machine, ARM emulation is enabled automatically
# (requires a docker daemon that allows privileged containers).
#
set -euo pipefail
cd "$(dirname "$0")"

# Enable ARM emulation for the build container (no-op if already enabled,
# e.g. in CI where docker/setup-qemu-action has run, or on an ARM host).
docker run --rm --privileged tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true

BUILDER_IMAGE="${BUILDER_IMAGE:-docker.io/unfoldedcircle/r2-pyinstaller:3.11.13-0.6.0}"
VERSION=$(sed -nE 's/.*"version": *"([^"]+)".*/\1/p' intg-acmeda/driver.json | head -1)
OUT="uc-intg-acmeda-${VERSION}-aarch64.tar.gz"

echo "Building $OUT ..."
docker run --rm --platform=linux/arm64/v8 --user root \
  -e OUT="$OUT" \
  -v "$PWD":/workspace \
  "$BUILDER_IMAGE" \
  bash -c '
    set -eo pipefail
    cd /workspace
    PYTHON_VERSION=$(python --version | cut -d" " -f2 | cut -d. -f1,2)
    pip install --user -r intg-acmeda/requirements.txt
    PYTHONPATH=$HOME/.local/lib/python${PYTHON_VERSION}/site-packages:${PYTHONPATH:-} \
      pyinstaller --clean --onedir --name intg-acmeda intg-acmeda/driver.py -y
    rm -rf artifacts
    mkdir -p artifacts
    cp -r dist/intg-acmeda artifacts/bin
    mv artifacts/bin/intg-acmeda artifacts/bin/driver
    cp intg-acmeda/driver.json LICENSE artifacts/
    tar czf "$OUT" -C artifacts .
    # The mount was written as root inside the container - make sure the
    # host user can read the archive and clean up the build directories.
    chmod -R a+rwX artifacts build dist "$OUT" ./*.spec 2>/dev/null || true
  '
echo "Built $OUT"
