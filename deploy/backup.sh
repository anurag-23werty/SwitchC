#!/usr/bin/env sh
# Back up everything a Khata deployment can't lose:
#   khata-<stamp>.dump      Khata's database (invoices, decisions, policy, audit log)
#   switch-<stamp>.sql.gz   Switch's database (rooms, messages, Mattermost)
#   payments-<stamp>.tgz    the NEFT drafts and journals Khata wrote
# Run from the repo root. Cron it daily, and copy BACKUP_DIR off the machine:
#   0 2 * * * cd /srv/khata && ./deploy/backup.sh >> backups/backup.log 2>&1
set -eu

DIR=${BACKUP_DIR:-./backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
COMPOSE=${COMPOSE:-docker compose}
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DIR"

$COMPOSE exec -T khata-db pg_dump -U khata -Fc khata > "$DIR/khata-$STAMP.dump"
$COMPOSE exec -T postgres sh -c 'pg_dumpall -U "$POSTGRES_USER"' | gzip > "$DIR/switch-$STAMP.sql.gz"

if [ -d data/out/payments ]; then
  tar -czf "$DIR/payments-$STAMP.tgz" -C data/out payments
elif $COMPOSE ps --services 2>/dev/null | grep -qx khata-serve; then
  $COMPOSE exec -T khata-serve tar -cz -C /app/data/out payments > "$DIR/payments-$STAMP.tgz"
fi

# A dump that can't be listed is not a backup.
$COMPOSE exec -T khata-db pg_restore --list < "$DIR/khata-$STAMP.dump" > /dev/null
gzip -t "$DIR/switch-$STAMP.sql.gz"

find "$DIR" -name 'khata-*.dump' -o -name 'switch-*.sql.gz' -o -name 'payments-*.tgz' \
  | while read -r f; do
      if [ -n "$(find "$f" -mtime +"$KEEP_DAYS")" ]; then rm -f "$f"; fi
    done
echo "backup $STAMP ok: $(ls -1 "$DIR" | grep -c "$STAMP") files in $DIR"
