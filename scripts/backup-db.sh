#!/usr/bin/env bash
# Snapshot the MyPhotos catalog DB.
#
# Default: takes a consistent SQLite copy via `sqlite3 .backup` (works
# while the worker is running — no need to stop services).
#
# If MariaDB is configured (database.url in local.toml or $DATABASE_URL),
# pass --mariadb to mysqldump the schema + data instead. Both can run
# side-by-side as belt-and-suspenders.
#
# Usage:
#   scripts/backup-db.sh                  # SQLite snapshot to data/backups/
#   scripts/backup-db.sh /mnt/backup      # custom output dir
#   scripts/backup-db.sh --mariadb        # mysqldump from configured DSN
#   scripts/backup-db.sh --both           # do both
#
# Output filename pattern:
#   catalog-YYYYMMDD-HHMMSS.db
#   catalog-YYYYMMDD-HHMMSS.sql.gz   (mariadb)

set -euo pipefail
cd "$(dirname "$0")/.."

mode="sqlite"
out=""
for arg in "$@"; do
  case "$arg" in
    --mariadb) mode="mariadb" ;;
    --both)    mode="both" ;;
    --sqlite)  mode="sqlite" ;;
    -*)        echo "unknown flag: $arg" >&2; exit 1 ;;
    *)         out="$arg" ;;
  esac
done
: "${out:=data/backups}"
mkdir -p "$out"
ts=$(date +%Y%m%d-%H%M%S)

backup_sqlite() {
  local src=data/catalog.db
  if [ ! -f "$src" ]; then
    echo "==> no sqlite catalog at $src — skipping" >&2
    return 0
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "==> sqlite3 CLI not found; falling back to plain file copy" >&2
    cp -p "$src" "$out/catalog-$ts.db"
  else
    sqlite3 "$src" ".backup $out/catalog-$ts.db"
  fi
  echo "==> sqlite: $out/catalog-$ts.db ($(du -h "$out/catalog-$ts.db" | cut -f1))"
}

backup_mariadb() {
  # Pull the URL from app.config (same source the runtime uses).
  local url
  url=$(.venv/bin/python -c 'from app.config import get_settings; print(get_settings().database.url or "")' 2>/dev/null || echo "")
  if [ -z "$url" ]; then
    echo "==> database.url not configured — skipping mariadb dump" >&2
    return 0
  fi
  if [[ "$url" != mysql* && "$url" != mariadb* ]]; then
    echo "==> database.url is not mysql/mariadb ($url) — skipping" >&2
    return 0
  fi
  if ! command -v mysqldump >/dev/null 2>&1; then
    echo "ERROR: mysqldump not found in PATH" >&2
    return 1
  fi
  # Parse mysql+pymysql://user:pass@host:port/dbname?charset=...
  local rest=${url#*://}
  local creds=${rest%%@*}
  local hostpart_db=${rest#*@}
  local hostpart=${hostpart_db%%/*}
  local dbname_q=${hostpart_db#*/}
  local dbname=${dbname_q%%\?*}
  local user=${creds%%:*}
  local pass=${creds#*:}
  local host=${hostpart%%:*}
  local port=${hostpart##*:}
  [ "$port" = "$hostpart" ] && port=3306
  local dst="$out/catalog-$ts.sql.gz"
  echo "==> mysqldump $host:$port/$dbname → $dst"
  MYSQL_PWD="$pass" mysqldump \
    --host="$host" --port="$port" --user="$user" \
    --single-transaction --quick --default-character-set=utf8mb4 \
    "$dbname" | gzip > "$dst"
  echo "==> mariadb: $dst ($(du -h "$dst" | cut -f1))"
}

case "$mode" in
  sqlite)  backup_sqlite ;;
  mariadb) backup_mariadb ;;
  both)    backup_sqlite; backup_mariadb ;;
esac

# Retain only the most recent 14 of each kind so the dir doesn't grow forever.
find "$out" -maxdepth 1 -name 'catalog-*.db'     -type f -printf '%T@ %p\n' \
  | sort -nr | tail -n +15 | awk '{print $2}' | xargs -r rm -f
find "$out" -maxdepth 1 -name 'catalog-*.sql.gz' -type f -printf '%T@ %p\n' \
  | sort -nr | tail -n +15 | awk '{print $2}' | xargs -r rm -f

echo "==> done"
