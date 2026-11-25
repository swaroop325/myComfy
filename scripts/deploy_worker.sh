#!/usr/bin/env bash
# Packages the seriesmaker worker and uploads it to s3://<bucket>/releases/seriesmaker-latest.tar.gz.
# The box installs it with `seriesmaker-update` (boot and on demand).
# Usage: scripts/deploy_worker.sh [--restart]   (--restart also restarts the worker over SSH)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$ROOT/terraform"
BUCKET="$(terraform -chdir="$TF" output -raw bucket)"
REGION="${AWS_REGION:-$(terraform -chdir="$TF" output -raw aws_region)}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/seriesmaker-src"
mkdir -p "$PKG"
cp "$ROOT/pyproject.toml" "$PKG/"
cp -R "$ROOT/src" "$PKG/src"
find "$PKG" \( -name '__pycache__' -o -name '*.egg-info' \) -type d -prune -exec rm -rf {} +
find "$PKG" -name '*.pyc' -delete
tar -C "$STAGE" -czf "$STAGE/seriesmaker-latest.tar.gz" seriesmaker-src

aws s3 cp --region "$REGION" "$STAGE/seriesmaker-latest.tar.gz" "s3://$BUCKET/releases/seriesmaker-latest.tar.gz"
echo "Uploaded s3://$BUCKET/releases/seriesmaker-latest.tar.gz"

if [ "${1:-}" = "--restart" ]; then
  IP="$(terraform -chdir="$TF" output -raw instance_public_ip 2>/dev/null || true)"
  if [ -z "$IP" ] || [ "$IP" = "null" ]; then
    echo "No public IP: the instance is stopped. Start it first (send the bot a message or: aws ec2 start-instances), run 'terraform -chdir=terraform apply -refresh-only', then retry." >&2
    exit 1
  fi
  KEY="$(terraform -chdir="$TF" output -raw key_file_path)"
  ssh -o StrictHostKeyChecking=accept-new -i "$KEY" "ubuntu@$IP" 'sudo systemctl restart seriesmaker-worker && systemctl is-active seriesmaker-worker'
else
  echo "On the box: sudo systemctl restart seriesmaker-worker (its ExecStartPre pulls this release)"
fi
