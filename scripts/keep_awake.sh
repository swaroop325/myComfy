#!/usr/bin/env bash
# Keeps the GPU box awake for <hours>: the reaper never stops the instance while the
# DynamoDB item META/HOLD holds a future epoch. Use it for the LLM speed spike,
# download-models.sh, a long first boot, or any manual SSH session.
# Usage: scripts/keep_awake.sh <hours>      (0 ends the hold now)
# No secrets: it writes one epoch number. The hold expires on its own.
set -euo pipefail
HOURS="${1:-}"
if ! [[ "$HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "usage: $0 <hours>   e.g. $0 3   (0 ends the hold)" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$ROOT/terraform"
REGION="${AWS_REGION:-$(terraform -chdir="$TF" output -raw aws_region)}"
TABLE="$(terraform -chdir="$TF" output -raw table)"
UNTIL="$(python3 -c 'import sys, time; print(int(time.time() + float(sys.argv[1]) * 3600))' "$HOURS")"

aws dynamodb put-item --region "$REGION" --table-name "$TABLE" \
  --item "{\"pk\":{\"S\":\"META\"},\"sk\":{\"S\":\"HOLD\"},\"at\":{\"N\":\"$UNTIL\"}}"

WHEN="$(python3 -c 'import sys, datetime; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))' "$UNTIL")"
echo "Keep-awake hold until $WHEN. The reaper will not stop the instance before then."
echo "Remember to stop it yourself afterwards (or run: $0 0 and let the reaper idle-stop it)."
