#!/usr/bin/env bash
#
# Build multi-architecture Docker images (amd64 / x86 and arm64) for
# uc-acmeda-pulse using docker buildx.
#
# Multi-arch images cannot be loaded into the local Docker image store; they
# have to be pushed to a registry. So:
#   - By default this builds both platforms (validates them) without output.
#   - Set PUSH=1 and IMAGE=<registry/name> to build and push a multi-arch image.
#   - Set LOAD=1 with a single PLATFORMS value to load one arch locally for testing.
#
# Examples:
#   ./build-docker.sh                                  # build & validate both arches
#   IMAGE=ghcr.io/dgaust/uc-acmeda-pulse TAG=0.2.2 PUSH=1 ./build-docker.sh
#   PLATFORMS=linux/amd64 LOAD=1 ./build-docker.sh     # load an x86 image locally
#
set -euo pipefail

IMAGE="${IMAGE:-uc-acmeda-pulse}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

cd "$(dirname "$0")"

# One-time (no-op if it already exists): a buildx builder that can do multi-arch.
if ! docker buildx inspect ucpulse >/dev/null 2>&1; then
  docker buildx create --name ucpulse --bootstrap >/dev/null
fi

# Emulation for cross-building arm64 on an x86 host (no-op if already set up).
docker run --rm --privileged tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true

args=(buildx build --builder ucpulse --platform "$PLATFORMS" -t "$IMAGE:$TAG")
if [ "${PUSH:-0}" = "1" ]; then
  args+=(--push)
elif [ "${LOAD:-0}" = "1" ]; then
  args+=(--load)
fi
args+=(.)

echo "Building $IMAGE:$TAG for $PLATFORMS ..."
docker "${args[@]}"
echo "Done."
