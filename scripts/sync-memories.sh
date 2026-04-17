#!/usr/bin/env bash
#
# Sync pyclaudir data between local and remote server.
#
# Usage:
#   ./scripts/sync-memories.sh pull user@server   # Server → local (memories + DB)
#   ./scripts/sync-memories.sh push user@server   # Local → server (project.md)
#   ./scripts/sync-memories.sh sync user@server   # Pull memories, then push project.md
#
# The remote path defaults to ~/pyclaudir. Override with REMOTE_DIR:
#   REMOTE_DIR=/opt/pyclaudir ./scripts/sync-memories.sh pull user@server

set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-~/pyclaudir}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    echo "Usage: $0 {pull|push|sync} user@server"
    echo ""
    echo "Commands:"
    echo "  pull   Pull memories, DB, and access.json from server to local"
    echo "  push   Push project.md and access.json from local to server"
    echo "  sync   Pull first, then push (bidirectional)"
    echo ""
    echo "Environment:"
    echo "  REMOTE_DIR   Remote pyclaudir directory (default: ~/pyclaudir)"
    exit 1
}

[[ $# -lt 2 ]] && usage

CMD="$1"
SERVER="$2"

pull() {
    echo "=== Pulling from $SERVER ==="

    echo "  memories..."
    rsync -avz --delete \
        "$SERVER:$REMOTE_DIR/data/memories/" \
        "$LOCAL_DIR/data/memories/"

    echo "  database..."
    rsync -avz \
        "$SERVER:$REMOTE_DIR/data/pyclaudir.db" \
        "$LOCAL_DIR/data/pyclaudir.db"

    echo "  access.json..."
    rsync -avz \
        "$SERVER:$REMOTE_DIR/data/access.json" \
        "$LOCAL_DIR/data/access.json" 2>/dev/null || true

    echo "=== Pull complete ==="
}

push() {
    echo "=== Pushing to $SERVER ==="

    echo "  project.md..."
    rsync -avz \
        "$LOCAL_DIR/prompts/project.md" \
        "$SERVER:$REMOTE_DIR/prompts/project.md"

    echo "  access.json..."
    rsync -avz \
        "$LOCAL_DIR/data/access.json" \
        "$SERVER:$REMOTE_DIR/data/access.json" 2>/dev/null || true

    echo "=== Push complete ==="
    echo "Note: restart the container for project.md changes to take effect:"
    echo "  ssh $SERVER 'cd $REMOTE_DIR && docker compose restart'"
}

case "$CMD" in
    pull)  pull ;;
    push)  push ;;
    sync)  pull; echo ""; push ;;
    *)     usage ;;
esac
