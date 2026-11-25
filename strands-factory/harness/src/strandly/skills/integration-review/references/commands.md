# Commands (all validated against harness-sdk#3998 on 2026-09-06)

```bash
# --- GitHub provenance ---
gh api users/$AUTHOR --jq '{login,created_at,public_repos,followers,company}'
gh api orgs/$OWNER --jq '{created_at,is_verified,public_repos,blog}'; gh api orgs/$OWNER/public_members --jq '[.[].login]'
gh api "repos/$OWNER/$REPO/contributors?per_page=100" --jq '[.[]|{login,contributions}]'
gh api repos/$OWNER/$REPO --jq '{created_at,pushed_at,stargazers_count,forks_count,open_issues_count,fork,archived,license:.license.spdx_id}'
gh api "repos/$OWNER/$REPO/commits?per_page=1" -i | grep -i '^link:' | grep -oE 'page=[0-9]+>; rel="last"'   # total commits
gh api "repos/$OWNER/$REPO/releases?per_page=100" --jq 'length'
gh api repos/$OWNER/$REPO/contents/SECURITY.md --jq '.size'; gh api "repos/$OWNER/$REPO/contents/.github/workflows" --jq '[.[].name]'

# --- PyPI ---
curl -s https://pypi.org/pypi/$PKG/json | python3 -c '
import json,sys; d=json.load(sys.stdin); i=d["info"]; r=d["releases"]
print(i["version"], i.get("license_expression") or i.get("license"), i.get("project_urls"))
print([x for x in (i.get("requires_dist") or []) if "strands" in x or "extra" in x])
print("releases",len(r),"first",min((f["upload_time"] for v in r.values() for f in v),default=None))
print("yanked",[v for v,fs in r.items() if fs and all(f.get("yanked") for f in fs)])
print([(u["filename"]) for u in d["urls"]])'
curl -s https://pypi.org/pypi/$PKG/$VER/json | python3 -c 'import json,sys;print(json.load(sys.stdin)["info"]["requires_dist"])'   # per-release pins
curl -s "https://pypi.org/integrity/$PKG/$VER/$FILENAME/provenance" | python3 -c 'import json,sys;print(json.load(sys.stdin)["attestation_bundles"][0]["publisher"])'   # 404 = no Trusted Publishing
curl -s https://pypistats.org/api/packages/$PKG/recent      # rate-limits (429) hard; fallback api.pepy.tech or site/src/data/catalog-stats.json
# --- npm ---
curl -s https://registry.npmjs.org/$NPM | python3 -c '
import json,sys; d=json.load(sys.stdin); lv=d["dist-tags"]["latest"]; v=d["versions"][lv]
print(lv, len(d["versions"]), d["time"]["created"], d.get("repository"), d.get("license"), v.get("peerDependencies"))
print({k:s for k,s in (v.get("scripts") or {}).items() if "install" in k or k=="prepare"}, (v.get("dist") or {}).get("attestations"))'
curl -s https://api.npmjs.org/downloads/point/last-month/$NPM
# --- advisories ---
curl -s -X POST https://api.osv.dev/v1/query -d '{"package":{"name":"'$PKG'","ecosystem":"PyPI"}}'   # {} = none; "npm" for npm

# --- artifact ≡ source, surface, hazards (Python) ---
python3 -m venv .v && . .v/bin/activate && pip download --no-deps -q "$PKG==$VER" -d . && unzip -q -o *.whl -d wheel
PKGDIR=$(ls wheel | grep -v dist-info | head -1)
git clone -q --depth 1 --branch v$VER https://github.com/$OWNER/$REPO repo || git clone -q --depth 1 --branch $VER https://github.com/$OWNER/$REPO repo
SRC=$([ -d repo/src/$PKGDIR ] && echo repo/src/$PKGDIR || echo repo/$PKGDIR); diff -rq wheel/$PKGDIR $SRC | grep -v __pycache__   # empty = identical
grep -rhoE "from strands[a-z._]* import [A-Za-z_, ]+" wheel/$PKGDIR | sort -u
grep -rnE "strands\._|setattr\((strands|Agent|Model)|monkeypatch|^class \w+\((Model|AgentTool|Plugin|HookProvider|InterventionHandler|SessionManager|RepositorySessionManager|SessionRepository|MemoryStore|Storage)\b" wheel/$PKGDIR
grep -rnE "\b(eval|exec)\(|marshal\.|pickle\.loads|subprocess|os\.system|b64decode|__import__\(|expanduser|\.aws|\.ssh|\.env\b" wheel/$PKGDIR
grep -nE "cmdclass|setup_requires|build\.hooks" repo/pyproject.toml repo/setup.py 2>/dev/null
# TypeScript: npm pack $NPM@$VER && tar xzf *.tgz; check package.json scripts for pre/postinstall; grep 'fetch(|axios|undici|node:http|https?://' package/

# --- egress ---
grep -rlE "^\s*(import|from)\s+(httpx|requests|aiohttp|urllib|socket|websockets?|boto3|botocore|grpc)\b" wheel/$PKGDIR
grep -rnoE "https?://[a-zA-Z0-9./_:-]+" wheel/$PKGDIR --include=*.py | grep -vE "apache.org|w3.org|json-schema|example\." | sort -u
grep -rhoE "environ(\.get)?\[?\(?['\"][A-Z_]+" wheel/$PKGDIR | grep -oE "[A-Z_]{3,}" | sort -u
grep -rnE "(base_url|endpoint|api_key|token|secret)\s*[:=]" wheel/$PKGDIR | head -30

# --- installability ---
pip index versions strands-agents 2>/dev/null | head -1
python3 -m venv .i && . .i/bin/activate && pip install -q strands-agents "$PKG[$EXTRA]" 2>&1 | grep -iE "conflict|incompatible|error"
env -u OTEL_PROPAGATORS python -c "import strands, $MODULE; from $MODULE import $CLASS; print('ok')" && pip check
#   (OTEL_PROPAGATORS unset: some CI images export `xray`, which breaks `import strands` unrelated to the package)

# --- guide-only / hosted ---
curl -s $URL -X POST -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 2000
curl -s -o /dev/null -w "%{http_code}\n" $BASE_URL/models
```
