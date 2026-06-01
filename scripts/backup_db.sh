#!/bin/bash
# Daily PostgreSQL backup with 7-day rotation.
# Reads DB credentials from config.json and the mounted password secret.
# Writes compressed dumps to /app/backups/.

set -euo pipefail

BACKUP_DIR="/app/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="ibis_${TIMESTAMP}.sql.gz"
KEEP_DAYS=7

DB_HOST="db"
DB_PORT="5432"
DB_NAME="ibis"
DB_USER="ibis_user"
DB_PASSWORD=$(cat /run/secrets/db_password)

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting backup → ${BACKUP_DIR}/${FILENAME}"

PGPASSWORD="$DB_PASSWORD" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    "$DB_NAME" | gzip > "${BACKUP_DIR}/${FILENAME}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete: $(du -h "${BACKUP_DIR}/${FILENAME}" | cut -f1)"

# Remove backups older than KEEP_DAYS days
find "$BACKUP_DIR" -name "ibis_*.sql.gz" -mtime +${KEEP_DAYS} -delete
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rotation done — kept last ${KEEP_DAYS} days of backups."
