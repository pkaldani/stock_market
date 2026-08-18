#!/usr/bin/env sh
# The app reads/writes accounts.db and memory/<name>.db as plain relative paths
# (see backend/database.py, backend/mcp_servers.py) — there is no env var to
# redirect them. To persist that state on a mounted volume without touching
# app source, point both paths at $DATA_DIR via symlinks before exec'ing.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/memory"
[ -e "$DATA_DIR/accounts.db" ] || : > "$DATA_DIR/accounts.db"

if [ -e accounts.db ] && [ ! -L accounts.db ]; then
  rm -f accounts.db
fi
ln -sfn "$DATA_DIR/accounts.db" accounts.db

if [ -e memory ] && [ ! -L memory ]; then
  rm -rf memory
fi
ln -sfn "$DATA_DIR/memory" memory

exec "$@"
