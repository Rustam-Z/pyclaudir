#!/usr/bin/env bash
#
# Sync pyclaudir data between local and remote server.
#
# Usage:
#   ./scripts/sync-memories.sh pull user@server                  # default ssh
#   ./scripts/sync-memories.sh pull user@server --key ~/.ssh/k   # custom key
#   ./scripts/sync-memories.sh pull user@server --password       # ssh prompts
#
# The remote path defaults to ~/pyclaudir. Override with REMOTE_DIR:
#   REMOTE_DIR='~/pyclaudir' ./scripts/sync-memories.sh pull user@server

set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-~/pyclaudir}"
LOCAL_DIR="$(pwd)"

usage() {
    echo "Usage: $0 {pull|push} user@server [--key <path> | --password]"
    echo ""
    echo "Commands:"
    echo "  pull   Pull project.md, memories, DB from server to local"
    echo "  push   Push project.md, memories, DB from local to server"
    echo ""
    echo "Auth (optional, default = standard ssh / agent):"
    echo "  --key <path>   Use this private key file (ssh -i)"
    echo "  --password     Force password auth; ssh prompts inline."
    echo "                 NOTE: prompts 3x per run (one per rsync call)."
    echo ""
    echo "Environment:"
    echo "  REMOTE_DIR     Remote pyclaudir directory (default: ~/pyclaudir)"
    exit 1
}

[[ $# -lt 2 ]] && usage

CMD="$1"
SERVER="$2"

RSYNC_E=()
case "${3:-}" in
    "")
        ;;
    --key)
        [[ $# -lt 4 ]] && usage
        RSYNC_E=(-e "ssh -i $4")
        ;;
    --password)
        RSYNC_E=(-e "ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no")
        ;;
    *)
        usage
        ;;
esac

pull() {
    echo "=== Pulling from $SERVER ==="

    echo "  project.md..."
    rsync -avz "${RSYNC_E[@]}" \
        "$SERVER:$REMOTE_DIR/prompts/project.md" \
        "$LOCAL_DIR/prompts/project.md"

    echo "  memories..."
    rsync -avz --delete "${RSYNC_E[@]}" \
        "$SERVER:$REMOTE_DIR/data/memories/" \
        "$LOCAL_DIR/data/memories/"

    echo "  database..."
    rsync -avz "${RSYNC_E[@]}" \
        "$SERVER:$REMOTE_DIR/data/pyclaudir.db" \
        "$LOCAL_DIR/data/pyclaudir.db"

    echo "=== Pull complete ==="
}

push() {
    echo "=== Pushing to $SERVER ==="

    echo "  project.md..."
    rsync -avz "${RSYNC_E[@]}" \
        "$LOCAL_DIR/prompts/project.md" \
        "$SERVER:$REMOTE_DIR/prompts/project.md"

    echo "  memories..."
    rsync -avz "${RSYNC_E[@]}" \
        "$LOCAL_DIR/data/memories/" \
        "$SERVER:$REMOTE_DIR/data/memories/"

    echo "  database..."
    rsync -avz "${RSYNC_E[@]}" \
        "$LOCAL_DIR/data/pyclaudir.db" \
        "$SERVER:$REMOTE_DIR/data/pyclaudir.db"

    echo "=== Push complete ==="
    echo "Note: restart the container for project.md changes to take effect:"
    echo "  ssh $SERVER 'cd $REMOTE_DIR && docker compose restart'"
}

case "$CMD" in
    pull)  pull ;;
    push)  push ;;
    *)     usage ;;
esac