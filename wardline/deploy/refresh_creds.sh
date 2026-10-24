#!/usr/bin/env bash
#
# deploy/refresh_creds.sh -- refresh the EC2 server's AWS credentials when the
# workshop participant session expires (CLAUDE.md section 3: no instance role,
# so the server reads temp creds from /home/ec2-user/wardline.env).
#
# Run from your LAPTOP (where the `wardline` profile has fresh creds), not on
# the instance. It copies the current temp creds into the instance's env file
# and restarts the service.
#
# Usage:
#   deploy/refresh_creds.sh <public-ip> [path-to-key.pem]
#
# Example:
#   deploy/refresh_creds.sh 32.192.240.107 /tmp/wardline-key.pem
#
set -euo pipefail

IP="${1:?usage: refresh_creds.sh <public-ip> [key.pem]}"
KEY="${2:-/tmp/wardline-key.pem}"
PROFILE="${AWS_PROFILE:-wardline}"
REGION="${AWS_REGION:-us-east-1}"

AK=$(aws configure get aws_access_key_id --profile "$PROFILE")
SK=$(aws configure get aws_secret_access_key --profile "$PROFILE")
ST=$(aws configure get aws_session_token --profile "$PROFILE")

if [[ -z "${AK}" || -z "${SK}" ]]; then
  echo "No credentials found for profile '$PROFILE'. Log in first." >&2
  exit 1
fi

# Preserve the non-credential settings; only the three AWS_* creds change.
# FRONTEND_BASE_URL is the nurse link in alert emails (alert_agent appends ?token=...).
printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_SESSION_TOKEN=%s\nAWS_REGION=%s\nDATA_BACKEND=dynamodb\nMODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0\nNOTIFY_DRY_RUN=%s\nDRY_RUN=%s\nFROM_EMAIL=%s\nNOTIFY_TO_OVERRIDE=%s\nFRONTEND_BASE_URL=%s\n' \
  "$AK" "$SK" "$ST" "$REGION" \
  "${NOTIFY_DRY_RUN:-1}" "${DRY_RUN:-1}" \
  "${FROM_EMAIL:-lintuwun@gmail.com}" "${NOTIFY_TO_OVERRIDE:-}" \
  "${FRONTEND_BASE_URL:-http://wardline-frontend-580066280520.s3-website-us-east-1.amazonaws.com/nurse}" \
  | ssh -o StrictHostKeyChecking=no -i "$KEY" "ec2-user@${IP}" \
      "cat > /home/ec2-user/wardline.env && chmod 600 /home/ec2-user/wardline.env && (sudo systemctl restart wardline 2>/dev/null && echo 'creds refreshed + service restarted' || echo 'creds refreshed (systemd not installed; restart uvicorn manually)')"
