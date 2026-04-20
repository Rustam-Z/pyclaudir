#!/usr/bin/env bash
#
# Sync pyclaudir data between local and remote server.
#
# Usage:
#   ./scripts/sync-memories.sh pull user@server   # Server → local (memories + DB)
#   ./scripts/sync-memories.sh push user@server   # Local → server (project.md + memories)
#
# The remote path defaults to ~/pyclaudir. Override with REMOTE_DIR:
#   REMOTE_DIR=/opt/pyclaudir ./scripts/sync-memories.sh pull user@server
#
# Authentication: the script uses SSH connection multiplexing, so you get
# ONE password prompt per invocation regardless of how many rsync calls
# run. For zero prompts, set up key-based auth once:
#   ssh-copy-id user@server

set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-~/pyclaudir}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    echo "Usage: $0 {pull|push} user@server"
    echo ""
    echo "Commands:"
    echo "  pull   Pull memories, DB from server to local"
    echo "  push   Push project.md, memories from local to server"
    echo ""
    echo "Environment:"
    echo "  REMOTE_DIR   Remote pyclaudir directory (default: ~/pyclaudir)"
    exit 1
}

[[ $# -lt 2 ]] && usage

CMD="$1"
SERVER="$2"

# SSH connection multiplexing: the first ssh/rsync opens a master socket,
# subsequent calls reuse it. Without this you'd get a password prompt
# per rsync call (two per invocation). Socket lives in a private temp
# dir and is cleanly closed on exit.
SSH_CONTROL_DIR="$(mktemp -d -t pyclaudir-sync.XXXXXX)"
SSH_CONTROL_PATH="$SSH_CONTROL_DIR/control"
cleanup() {
    ssh -o "ControlPath=$SSH_CONTROL_PATH" -O exit "$SERVER" 2>/dev/null || true
    rm -rf "$SSH_CONTROL_DIR"
}
trap cleanup EXIT INT TERM

SSH_OPTS="-o ControlMaster=auto -o ControlPath=$SSH_CONTROL_PATH -o ControlPersist=60"
export RSYNC_RSH="ssh $SSH_OPTS"

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

    echo "=== Pull complete ==="
}

push() {
    echo "=== Pushing to $SERVER ==="

    echo "  project.md..."
    rsync -avz \
        "$LOCAL_DIR/prompts/project.md" \
        "$SERVER:$REMOTE_DIR/prompts/project.md"

    echo "  memories..."
    rsync -avz \
        "$LOCAL_DIR/data/memories/" \
        "$SERVER:$REMOTE_DIR/data/memories/"

    echo "=== Push complete ==="
    echo "Note: restart the container for project.md changes to take effect:"
    echo "  ssh $SERVER 'cd $REMOTE_DIR && docker compose restart'"
}

case "$CMD" in
    pull)  pull ;;
    push)  push ;;
    *)     usage ;;
esac
