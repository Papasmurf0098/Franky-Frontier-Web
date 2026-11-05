#!/usr/bin/env bash
# Publish a built site to the gh-pages branch, which GitHub Pages serves.
#   ci/publish.sh dist
# That branch only ever holds generated output, so each publish replaces it with one commit.
# Forcing is safe here and only here. Scan history lives on scan-state and is never forced.
set -euo pipefail

site="${1:?usage: publish.sh SITE_DIR}"
branch="${PAGES_BRANCH:-gh-pages}"
work=".pages"

if [ ! -f "$site/index.html" ]; then
  echo "No built site at $site." >&2
  exit 1
fi
site="$(cd "$site" && pwd)"

git worktree add --quiet --detach "$work"
trap 'git worktree remove --force "$work"' EXIT

git -C "$work" rm -rfq --ignore-unmatch .
cp -R "$site"/. "$work"/
git -C "$work" add -A
tree="$(git -C "$work" write-tree)"
commit="$(git -C "$work" \
  -c user.name='github-actions[bot]' \
  -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
  commit-tree "$tree" -m "Publish site $(date -u +%Y-%m-%dT%H:%MZ)")"
git -C "$work" push --quiet --force origin "$commit:refs/heads/$branch"
echo "Published to $branch."
