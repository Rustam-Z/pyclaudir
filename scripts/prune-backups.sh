#!/usr/bin/env bash
#
# Prune old prompt backups.
#
# Every successful write_instructions / append_instructions call
# writes a timestamped copy of the previous content to
# data/prompt_backups/<name>-<UTC timestamp>.md. After a year of
# daily self-reflection promotions that's ~365 small files. This
# script keeps the N most-recent backups per prompt file and moves
# older ones to data/prompt_backups/archive/.
#
# Usage:
#   ./scripts/prune-backups.sh                 # dry-run by default
#   ./scripts/prune-backups.sh --apply         # actually move files
#   ./scripts/prune-backups.sh --apply --keep 20
#
# Safe to run repeatedly. No files are ever deleted — only moved.

set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$LOCAL_DIR/data/prompt_backups"
ARCHIVE_DIR="$BACKUP_DIR/archive"

KEEP=50
APPLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --keep) KEEP="$2"; shift 2 ;;
        -h|--help)
            sed -n 's/^# \{0,1\}//p' "$0" | sed -n '3,15p'
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "no backup dir at $BACKUP_DIR — nothing to prune"
    exit 0
fi

mkdir -p "$ARCHIVE_DIR"

# Per-prompt-file: list backups matching <name>-*.md sorted newest first,
# skip the first KEEP, move the rest to archive/.
PROMPTS=(system project)
MOVED=0
for name in "${PROMPTS[@]}"; do
    # Find matching files at the top level only, excluding archive/.
    mapfile -t files < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name "${name}-*.md" | sort -r)
    total=${#files[@]}
    if (( total <= KEEP )); then
        echo "$name: $total backup(s), within KEEP=$KEEP — nothing to do"
        continue
    fi
    to_archive=$(( total - KEEP ))
    echo "$name: $total backup(s), keeping newest $KEEP, archiving $to_archive"
    for (( i=KEEP; i<total; i++ )); do
        src="${files[i]}"
        dst="$ARCHIVE_DIR/$(basename "$src")"
        if (( APPLY )); then
            mv -- "$src" "$dst"
        else
            echo "  would archive: $(basename "$src")"
        fi
        MOVED=$(( MOVED + 1 ))
    done
done

if (( APPLY )); then
    echo "=== Done. Moved $MOVED file(s) to $ARCHIVE_DIR"
else
    echo "=== Dry-run. Re-run with --apply to actually move $MOVED file(s)."
fi
