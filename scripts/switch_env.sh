#!/usr/bin/env bash
# Create switch/stack/.env from env.template with fresh random secrets.
# Refuses to overwrite an existing .env: the server's secrets and its
# registration token must stay stable once the stack has started.
set -euo pipefail
cd "$(dirname "$0")/../switch/stack"

if [[ -f .env ]]; then
  echo "switch/stack/.env already exists - leaving it alone." >&2
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

cp env.template .env
for key in DB_PASSWORD DB_OWNER_PASSWORD AGENT_REGISTRATION_TOKEN \
           GATEWAY_ADMIN_PASSWORD MATTERMOST_ADMIN_PASSWORD MATTERMOST_USER_PASSWORD; do
  sed -i.bak "s|^${key}=.*|${key}=$(openssl rand -hex 24)|" .env
done
sed -i.bak "s|^SECRET_KEYS=.*|SECRET_KEYS=khata-local:$(openssl rand -hex 32)|" .env
rm -f .env.bak
echo "Wrote switch/stack/.env"
