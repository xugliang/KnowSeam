#!/usr/bin/env bash
set -euo pipefail
[ $# = 2 ] || { echo 'usage: docling-upstream-update.sh REF EXPECTED_SHA' >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo 'dirty worktree; preserve and commit changes first' >&2; exit 1; }
[[ $1 =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && $2 =~ ^[0-9a-f]{40}$ ]] || { echo 'invalid ref/SHA' >&2; exit 2; }
git fetch https://github.com/docling-project/docling.git "refs/tags/$1"
[ "$(git rev-parse 'FETCH_HEAD^{commit}')" = "$2" ] || { echo 'SHA mismatch' >&2; exit 1; }
git switch -c "upgrade/docling-$1"
git subtree merge --prefix third_party/docling "$2" --squash
echo 'Review conflicts, refresh provenance and dependency/model locks, test, then commit. No deployment performed.'
