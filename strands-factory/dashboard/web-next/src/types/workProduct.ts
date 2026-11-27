/**
 * WorkProduct + Finding types — hand-mirrored from the dataclasses and enum vocabularies in
 * harness/src/strandly/ops/work_product.py. The API serializes WorkProduct to snake_case matching the
 * DynamoDB item attributes, so these field names are snake_case on purpose — they are wire names, not JS
 * conventions. Decimals (confidence, evidence numbers) arrive as plain numbers.
 *
 * If the contract changes, update this file (it is the single source of truth for the frontend's
 * shape) — there is no codegen step wired yet; that is a deliberate follow-up (see README).
 */

// §1 kind
export type WorkProductKind = "review" | "triage" | "first-response";

// §1 status
export type WorkProductStatus = "draft" | "curated" | "posted";

// §4 verdict vocabularies (kind-dependent). Kept as a union of every enum so a single `verdict`
// field types across kinds; the UI narrows by `kind`.
export type ReviewVerdict = "approve" | "changes-requested";
export type TriageVerdict = "accept" | "defer" | "redirect" | "reject" | "escalate";
export type FirstResponseDisposition =
  | "needs-info"
  | "confirmed-bug"
  | "duplicate"
  | "wrong-repo"
  | "works-as-intended"
  | "escalate";
export type Verdict = ReviewVerdict | TriageVerdict | FirstResponseDisposition | string;

// §4 first-response
export type ReproStatus = "reproduced" | "partial" | "not-reproduced" | "needs-info" | string;
export type Priority = "P0" | "P1" | "P2" | "P3" | string;

// §1 Finding tiers → 🔴/🟡/⚪
export type FindingTier = "block" | "should-fix" | "nit";

/** §1 Finding (a list item on a review WorkProduct). */
export interface Finding {
  id: string;
  file: string;
  line: number;
  tier: FindingTier;
  category: string; // e.g. "correctness", "api-shape"
  confidence: number; // 0.0–1.0
  text: string;
  suggestion?: string; // GitHub `suggestion` block body, if any
  reachability?: string;
  suppressed?: boolean; // aggregator chose not to post it
  suppress_reason?: string;
  /**
   * Curation decision — is it in the posting set. Contract default is "not suppressed".
   * Optional on the wire; `effectiveIncluded()` in lib/curation.ts applies the default.
   */
  included?: boolean;
}

/** §1 evidence_ledger. */
export interface EvidenceLedger {
  branch_sha?: string;
  tests_cmd?: string;
  tests_passed?: number;
  repros?: number;
}

/** §1 WorkProduct record. */
export interface WorkProduct {
  work_product_id: string;
  session_id: string;
  kind: WorkProductKind;
  status: WorkProductStatus;
  created_at: string; // ISO
  updated_at: string; // ISO
  verdict: Verdict;
  title?: string; // the GitHub item's human title; the portal renders this, `repo#number` as a dimmed ref
  target_url: string;
  repo: string; // "owner/repo"
  number: number;
  tldr: string;
  findings?: Finding[];
  evidence_ledger?: EvidenceLedger;
  recommended_labels?: string[];
  draft_comment?: string;
  priority?: Priority;
  repro_status?: ReproStatus;
  suspected_area?: string; // first-response: file:line or component, when known
  duplicates?: string[];
  run_task_id?: string;
  curated_by?: string;
  posted_at?: string;
  github_review_id?: string;
}

/** §6 list-endpoint envelope. */
export interface WorkProductList {
  items: WorkProduct[];
}
