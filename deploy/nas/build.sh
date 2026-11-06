#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
revision=${MA_REVISION:-$(git rev-parse HEAD)}
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo "Expected a full commit SHA" >&2; exit 1; }
image=${1:?Usage: build.sh IMAGE}
docker build --build-arg MA_REVISION="$revision" \
  --build-arg MASS_VERSION="2.10.9+g${revision:0:12}" \
  -f deploy/nas/Dockerfile -t "$image" .
