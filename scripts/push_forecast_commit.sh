#!/usr/bin/env bash
# The runner has one reviewed publication commit on a shallow checkout.
set -euo pipefail

base=$(git rev-parse HEAD^)
for attempt in 1 2 3; do
    if git push origin HEAD:master; then
        exit 0
    fi
    if [ "$attempt" -eq 3 ]; then
        break
    fi

    git fetch --no-tags --depth=1 origin refs/heads/master
    remote=$(git rev-parse FETCH_HEAD)
    if [ "$remote" = "$(git rev-parse HEAD)" ]; then
        exit 0
    fi
    if [ "$remote" != "$base" ]; then
        # Replay just the reviewed commit, including when shallow tips share no visible history.
        if ! git rebase --autostash --onto "$remote" "$base"; then
            git rebase --abort
            exit 1
        fi
        base=$remote
    fi
done
exit 1
