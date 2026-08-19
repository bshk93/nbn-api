#!/usr/bin/env bash
# deploy.sh — pull main into the live checkout and restart the service.
#
# Unlike the site, a pull alone changes nothing here: uvicorn holds the old
# modules until it is restarted, so the restart *is* the deploy and skipping it
# leaves live running code that no longer matches the tree — the worst of both.
#
# The dirty-tree and --ff-only guards are the same as nbn-today/deploy.sh, for
# the same reasons; see the comment there.
#
# Rollback: git reset --hard <previous-sha> && sudo systemctl restart nbn-api.
# Code only — league data lives in /var/lib/nothing-but-stats and is recovered
# from its own git repo (docs/dev-deploy-setup-spec.md).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "REFUSING TO DEPLOY: the live tree has uncommitted changes." >&2
    git status --short >&2
    exit 1
fi

BEFORE=$(git rev-parse --short HEAD)
git pull --ff-only
AFTER=$(git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "already up to date at $AFTER — restarting anyway is not free, skipping"
    exit 0
fi

echo "deployed $BEFORE -> $AFTER"
git --no-pager log --oneline "$BEFORE..$AFTER"

sudo systemctl restart nbn-api
sleep 2
if systemctl is-active --quiet nbn-api; then
    echo "nbn-api restarted and active"
else
    echo "!!! nbn-api is NOT active after restart — journalctl -u nbn-api -n 50" >&2
    exit 1
fi
echo
echo "Rollback: git reset --hard $BEFORE && sudo systemctl restart nbn-api"
