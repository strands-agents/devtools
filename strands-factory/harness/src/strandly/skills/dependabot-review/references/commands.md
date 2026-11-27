# Commands (registry lookups validated 2026-10-15; repo test commands from each repo's CI)

```bash
# --- the PR ---
gh pr view $N --repo $R --json title,body,headRefOid,files,labels,isDraft,state,statusCheckRollup
gh api repos/$R/pulls/$N/reviews --jq '.[] | select(.user.login == "'$ME'") | {commit_id, submitted_at, body: .body[0:120]}'  # already reviewed? ME=$(gh api user --jq .login)
gh pr list --repo $R --author app/dependabot --state open --json number,title  # superseded?
gh api repos/$OWNER/$REPO/releases --paginate --jq '.[]|{tag_name,published_at}'  # upstream notes
gh api repos/$OWNER/$REPO/releases/tags/$TAG --jq .body

# --- PyPI ---
curl -s https://pypi.org/pypi/$PKG/$VER/json | python3 -c 'import json,sys;d=json.load(sys.stdin);i=d["info"]
print(i["requires_python"], i["requires_dist"], d["urls"][0]["upload_time"], [u["yanked"] for u in d["urls"]], i["project_urls"])'
curl -s -o /dev/null -w '%{http_code}\n' "https://pypi.org/integrity/$PKG/$VER/$FILENAME/provenance"  # 404 = no attestation
for v in $OLD $NEW; do pip download --no-deps -q "$PKG==$v" -d "pkg/$v"; done   # one dir per version
for v in $OLD $NEW; do (cd pkg/$v && unzip -q -o *.whl); done; diff -ru pkg/$OLD pkg/$NEW -x '*.dist-info' -x '*.whl' | head -500

# --- npm ---
npm diff --diff=$PKG@$OLD --diff=$PKG@$NEW --diff-name-only
npm diff --diff=$PKG@$OLD --diff=$PKG@$NEW -- '*.d.ts' CHANGELOG.md
curl -s https://registry.npmjs.org/$PKG/$NEW | python3 -c 'import json,sys;v=json.load(sys.stdin)
print(v.get("engines"), {k:s for k,s in (v.get("scripts") or {}).items() if "install" in k}, bool((v.get("dist") or {}).get("attestations")))'
git diff origin/main...HEAD -- package-lock.json | grep -E '^\+.*("hasInstallScript"|"resolved")'  # new install scripts / packages

# --- crates.io ---
curl -s -A dependabot-review https://crates.io/api/v1/crates/$CRATE/$VER | python3 -c 'import json,sys;v=json.load(sys.stdin)["version"];print(v["rust_version"],v["created_at"],v["yanked"])'
for v in $OLD $NEW; do mkdir -p crate/$v && curl -sL https://static.crates.io/crates/$CRATE/$CRATE-$v.crate | tar -xz -C crate/$v; done
diff -ru crate/$OLD/$CRATE-$OLD crate/$NEW/$CRATE-$NEW | head -500
git diff origin/main...HEAD -- Cargo.lock | grep -E '^\+name = '   # new transitive crates

# --- GitHub Actions ---
gh api "repos/$OWNER/$ACTION/contents/action.yml?ref=$NEW_REF" --jq .content | base64 -d | grep -A2 '^runs:'

# --- advisories ---
curl -s -X POST https://api.osv.dev/v1/query -d '{"version":"'$NEW'","package":{"name":"'$PKG'","ecosystem":"PyPI"}}'  # npm / crates.io
```

## Per repo

| Repo | Ecosystems (dependabot.yml) | Test it |
|---|---|---|
| `strands-agents/harness-sdk` | pip `/strands-py`, `/strands-mcp` (range changes, no lockfile); npm `/` (strands-ts), `/site`, `/.github/scripts/pr-metrics/tools`; actions | Py: `cd strands-py && hatch fmt --linter && hatch test` (`STRANDS_TEST_MCP_V1=1` for the mcp-1.x env). TS: `npm ci && npm test && npm run check`. Site: `cd site && npm ci && npm run build && npm test` (slow; regenerates API docs). Integ: AWS-gated, skip. |
| `strands-agents/evals` | pip `/` (range changes, no lockfile); actions | `hatch fmt --linter && hatch test`. `hatch run test-integ` needs AWS, skip. Unit tests mock provider SDKs: a provider bump needs the call-site check, not just green tests. |
| `strands-agents/box` | cargo `/`, `/test-common`, `/test-integ`, `/test-workload/verdict`; actions | `cargo test --workspace --locked --all-features`, `cargo clippy --workspace --all-features`. Grep output for `skipping:` (feature/interpreter-gated tests). Containment tests are macOS/arm only. |
| `strands-agents/shell` | cargo `/`, pip `/` (maturin), npm `/` (devDeps only, no lockfile); actions | Rust: `cargo test --workspace --all-targets`. Py: `pip install maturin pytest && maturin develop --release && pytest tests/python`. Node: `npm install && npm run build:debug && npm test`. Lockfile bumps change the shipped wheels and npm binaries. |
