# Publishing to `charlie-protocol-v1`

The public repo is a **filtered publish** of this one, not a branch. The two
histories can never be merged — treat this repo as the source and the public one
as a build output.

## What differs

| | here | public |
|---|---|---|
| `.planning/` | present | stripped from every commit |
| author emails | the committer's real address | rewritten to `needsmorergb@users.noreply.github.com` |
| everything else | identical | identical |

Keeping "everything else identical" is the whole point. If a doc is edited in the
published repo and not here, the next publish silently reverts it — that already
happened once with the spec files and the READMEs.

## The recipe

Work in a throwaway clone so a bad filter cannot damage this repo.

```bash
git clone . /tmp/cp-publish && cd /tmp/cp-publish
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --prune-empty \
  --index-filter 'git rm -r --cached --ignore-unmatch -q .planning' \
  --env-filter '
    export GIT_AUTHOR_EMAIL="needsmorergb@users.noreply.github.com"
    export GIT_COMMITTER_EMAIL="needsmorergb@users.noreply.github.com"
  ' -- --all
git remote remove origin
rm -rf .git/refs/original .git/logs && git gc --prune=now
```

Then check, before pushing:

- `git log --all --format='%ae' | sort -u` — one address, the noreply one
- `git log --all --pretty=format: --name-only | grep '^\.planning'` — empty
- every markdown link resolves (the filter breaks links into `.planning/`)
- `python -m unittest discover -s tests -t tests` — 172 pass

Push to `needsmorergb/charlie-protocol-v1`, branch `main`.

## Gotcha

Filtering drops commits that touched only `.planning/`, so the public commit
count is much lower than this repo's. That is expected, not a lost commit.
