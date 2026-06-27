#!/usr/bin/env bash
# Build and deploy flatbot to a Raspberry Pi over SSH.
#
# Usage:
#   ./scripts/ship-to-pi.sh [user@host]
#
# Examples:
#   ./scripts/ship-to-pi.sh pi@raspberrypi.local
#   ./scripts/ship-to-pi.sh pi@192.168.1.42
#
# Prerequisites:
#   - Docker with buildx and the linux/arm64 builder set up locally
#   - SSH access to the Pi (key-based auth recommended)
#   - Docker installed on the Pi
#   - A .env file in the project root

set -euo pipefail

PI="${1:-pi@raspberrypi.local}"
REMOTE_DIR="~/flatbot"
IMAGE="flatbot:latest"

echo "==> Building linux/arm64 image..."
docker buildx build \
  --platform linux/arm64 \
  --tag "$IMAGE" \
  --load \
  .

echo "==> Saving image and streaming to Pi..."
docker save "$IMAGE" | ssh "$PI" "docker load"

echo "==> Copying compose file and .env to Pi..."
ssh "$PI" "mkdir -p $REMOTE_DIR"
scp docker-compose.yml "$PI:$REMOTE_DIR/docker-compose.yml"
scp .env "$PI:$REMOTE_DIR/.env"

echo "==> Restarting services on Pi..."
ssh "$PI" "cd $REMOTE_DIR && docker compose up -d"

echo "==> Done. Tailing logs (Ctrl-C to exit)..."
ssh "$PI" "cd $REMOTE_DIR && docker compose logs -f flatbot"
