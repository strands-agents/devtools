/**
 * Portal scope config — the per-user, in-browser knobs that retarget the whole portal at a
 * different set of orgs / maintainers team / focus folders, WITHOUT any code change or redeploy.
 *
 * Mirrors src/github/token.ts's store shape exactly: a localStorage-backed value with a
 * get / set / subscribe trio, driven reactively by `useSyncExternalStore`. Where token.ts stores a
 * bare string, this stores a JSON object; the getter therefore caches the parsed+normalized value
 * keyed on the raw string, so `getScope()` returns a STABLE reference across renders (a fresh object
 * every call would make `useSyncExternalStore` loop forever).
 *
 * Defaults come from the deployment's runtime config, not a hardcoded installation.
 * Saved per-user overrides retain the same localStorage key and precedence.
 */
import { DEFAULT_TEAM_SCOPE } from "./team";

/** The full configurable scope. All fields have defaults; a partial saved blob is merged over them. */
export interface Scope {
  /** Orgs to scan (each a bare org login, e.g. `strands-agents`). Drives every org-scoped search. */
  orgs: string[];
  /** Org that owns the maintainers team whose membership is the live roster. */
  maintainersOrg: string;
  /** Slug of the maintainers team within {@link maintainersOrg}. */
  maintainersTeamSlug: string;
}

/** The built-in defaults — equal to the (still-exported) module constants. */
export const DEFAULT_SCOPE: Scope = DEFAULT_TEAM_SCOPE;

const STORAGE_KEY = "strandly.scope";

type Listener = () => void;
const listeners = new Set<Listener>();

/** Trim + drop-empty + case-insensitive-dedupe a list of strings (order preserved, first wins). */
function normalizeList(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    if (typeof v !== "string") continue;
    const t = v.trim();
    if (!t) continue;
    const key = t.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out;
}

/**
 * Merge a parsed (untrusted) blob over the defaults and normalize every field. Empty/invalid
 * sections fall back to the default for that field, so a partially-edited or corrupt blob can never
 * leave the portal with, say, zero orgs (which would search nothing).
 */
function normalizeScope(raw: unknown): Scope {
  const r = (raw && typeof raw === "object" ? raw : {}) as Partial<Scope>;
  const orgs = normalizeList(r.orgs);
  const maintainersOrg =
    typeof r.maintainersOrg === "string" && r.maintainersOrg.trim()
      ? r.maintainersOrg.trim()
      : DEFAULT_SCOPE.maintainersOrg;
  const maintainersTeamSlug =
    typeof r.maintainersTeamSlug === "string" && r.maintainersTeamSlug.trim()
      ? r.maintainersTeamSlug.trim()
      : DEFAULT_SCOPE.maintainersTeamSlug;
  return {
    orgs: orgs.length ? orgs : [...DEFAULT_SCOPE.orgs],
    maintainersOrg,
    maintainersTeamSlug,
  };
}

/**
 * Cache so `getScope()` is referentially stable between renders: re-parse only when the underlying
 * raw string changes. `raw === undefined` means "not yet read"; `null` means "no value saved".
 */
let cache: { raw: string | null; value: Scope } | null = null;

function readRaw(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** The current scope (defaults merged with any saved overrides). Stable reference per raw value. */
export function getScope(): Scope {
  const raw = readRaw();
  if (cache && cache.raw === raw) return cache.value;
  let value: Scope;
  if (raw === null) {
    value = normalizeScope(undefined);
  } else {
    try {
      value = normalizeScope(JSON.parse(raw));
    } catch {
      value = normalizeScope(undefined);
    }
  }
  cache = { raw, value };
  return value;
}

/**
 * A stable string identity for the current scope — use as a `useMemo`/effect dependency to rebuild
 * scope-dependent objects (the GitHub client) when the scope changes. Mirrors how the raw token
 * string drives the client memo in GitHubContext.
 */
export function scopeKey(): string {
  return readRaw() ?? "";
}

/** Persist a normalized scope and notify subscribers. */
export function setScope(scope: Scope): void {
  const normalized = normalizeScope(scope);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // localStorage unavailable (private mode / blocked) — the scope just won't persist this session.
  }
  cache = null; // force a re-read on the next getScope()
  listeners.forEach((l) => l());
}

/** Clear all overrides — back to the built-in defaults. Notifies subscribers. */
export function resetScope(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore — nothing to clear if storage is unavailable
  }
  cache = null;
  listeners.forEach((l) => l());
}

/** Subscribe to scope changes. Returns an unsubscribe fn. Mirrors subscribeGithubToken. */
export function subscribeScope(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
