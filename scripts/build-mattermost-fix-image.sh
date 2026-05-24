#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
IMAGE="${IMAGE:-ghcr.io/doumin9383/hermes-agent:mattermost-thread-fix}"
SHA_IMAGE="${SHA_IMAGE:-ghcr.io/doumin9383/hermes-agent:mattermost-thread-fix-${GIT_SHA}}"

cd "$REPO_ROOT"

echo "Building $IMAGE and $SHA_IMAGE"
docker build -t "$IMAGE" -t "$SHA_IMAGE" .

echo "Pushing $IMAGE"
docker push "$IMAGE"
echo "Pushing $SHA_IMAGE"
docker push "$SHA_IMAGE"

echo "$IMAGE"
echo "$SHA_IMAGE"
