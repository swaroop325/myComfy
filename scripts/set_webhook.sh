#!/usr/bin/env bash
# Registers the Telegram webhook using values from SSM and terraform outputs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$ROOT/terraform"
REGION="${AWS_REGION:-$(terraform -chdir="$TF" output -raw aws_region)}"
WEBHOOK_URL="$(terraform -chdir="$TF" output -raw webhook_url)"
TOKEN="$(aws ssm get-parameter --region "$REGION" --name /seriesmaker/telegram_token --with-decryption --query Parameter.Value --output text)"
SECRET="$(aws ssm get-parameter --region "$REGION" --name /seriesmaker/webhook_secret --with-decryption --query Parameter.Value --output text)"

curl -fsS -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
echo
