#!/usr/bin/env bash
# Fetch and push the scan history, which lives on its own branch so main stays code only.
#   ci/state.sh restore .state    check the history out into a folder
#   ci/state.sh save .state       commit and push whatever changed
set -euo pipefail

action="${1:?usage: state.sh restore|save DIR}"
dir="${2:?usage: state.sh restore|save DIR}"
branch="${STATE_BRANCH:-scan-state}"

case "$action" in
  restore)
    # ls-remote separates "no branch yet" (exit 2) from "couldn't reach GitHub" (anything else).
    # Starting fresh after a network error would mean losing the history.
    status=0
    git ls-remote --exit-code --heads origin "$branch" >/dev/null || status=$?
    if [ "$status" -eq 0 ]; then
      git fetch --quiet --depth=1 origin "$branch"
      git worktree add --quiet --detach "$dir" FETCH_HEAD
      echo "Restored history from $branch."
    elif [ "$status" -eq 2 ]; then
      git worktree add --quiet --detach "$dir"
      git -C "$dir" checkout --quiet --orphan "$branch"
      git -C "$dir" rm -rfq --ignore-unmatch .
      echo "No $branch branch yet. Starting a new history."
    else
      echo "Could not reach the remote (git exit $status)." >&2
      exit 1
    fi
    ;;
  save)
    git -C "$dir" add -A
    if git -C "$dir" diff --cached --quiet; then
      echo "History unchanged."
      exit 0
    fi
    git -C "$dir" \
      -c user.name='github-actions[bot]' \
      -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
      commit --quiet -m "Scan $(date -u +%Y-%m-%dT%H:%MZ)"
    # Never forced. If this is rejected, the job fails and the history stays as it was.
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "History saved to $branch."
    ;;
  *)
    echo "Unknown action: $action" >&2
    exit 64
    ;;
esac
