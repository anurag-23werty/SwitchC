#!/usr/bin/env sh
# Restore Khata's database from a backup made by deploy/backup.sh.
#   ./deploy/restore.sh backups/khata-20261112-020000.dump
# Stop the agents first (they write to the database): docker compose stop khata-serve
# Switch's database restores with:  gunzip -c backups/switch-<stamp>.sql.gz |
#   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres'
set -eu
FILE=${1:?usage: deploy/restore.sh backups/khata-<stamp>.dump}
COMPOSE=${COMPOSE:-docker compose}
printf 'This replaces Khata'"'"'s database with %s. Type "restore" to continue: ' "$FILE"
read -r answer
[ "$answer" = "restore" ] || { echo "cancelled"; exit 1; }
$COMPOSE exec -T khata-db pg_restore -U khata -d khata --clean --if-exists --no-owner < "$FILE"
echo "restored $FILE; check with: uv run khata db status && uv run khata vendors"
