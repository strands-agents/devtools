/**
 * Resolve the pull request head commit in the authorization job so the run acts
 * on the commit the command was issued against, not whatever the branch resolves
 * to later.
 *
 * For a /strands comment it also confirms the head has not moved since the
 * comment: it reads the head branch's most recent entry in the Repository
 * Activity API (`GET /repos/{o}/{r}/activity`), whose per-ref-update `timestamp`
 * is server-assigned, and stops if that update happened at or after the comment.
 * The activity timestamp is used rather than the commit's own dates because
 * `committedDate` is client-supplied and `pushedDate` is deprecated (now null).
 *
 * Returns null (never throws) when there is nothing to resolve -- a non-PR
 * target, or an event with no head -- so it is safe to run in every caller's
 * authorization job. Stops (throws) only when it cannot confirm the head for an
 * actual /strands comment.
 *
 * @param {object} context - GitHub Actions context
 * @param {object} github - authenticated Octokit client
 * @param {object} options
 * @param {string} [options.issueId] - PR number; falls back to the comment/PR from the event
 * @returns {Promise<{headSha:string, headRepo:string, headRef:string}|null>}
 */
async function pinAndVerify(context, github, options) {
  const issueId = options.issueId
    || context.payload.issue?.number
    || context.payload.pull_request?.number;
  if (!issueId) {
    return null;
  }

  const commentedAt = context.payload.comment?.created_at;

  let pr;
  try {
    pr = await github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: Number(issueId),
    });
  } catch (error) {
    // 404 => not a PR (a /strands comment on a plain issue). Other resolution
    // errors are fatal only on the comment path, where we must not fall through
    // to resolving live HEAD.
    if (error.status === 404 || !commentedAt) {
      return null;
    }
    throw error;
  }

  if (!pr.data.head?.sha) {
    return null;
  }

  const headSha = pr.data.head.sha;
  const headRef = pr.data.head.ref;
  const headRepo = pr.data.head.repo?.full_name
    || `${context.repo.owner}/${context.repo.repo}`;

  // No triggering comment (auto-review / dispatch / auth-only): the caller
  // already has the exact commit, so just pin it -- there is no comment to check
  // the head against.
  if (!commentedAt) {
    console.log(`No triggering comment; pinning head ${headSha}`);
    return { headSha, headRepo, headRef };
  }

  // Confirm the head has not moved since the comment. The branch lives in the
  // head repo (a fork for a cross-repo PR), so read that repo's activity for the
  // head ref. Activity timestamps are populated for commits created any way
  // (push, API/bot, web, merge), unlike the now-null pushedDate.
  const [owner, name] = headRepo.split('/');
  const ref = `refs/heads/${headRef}`;
  // `ref` filters server-side, so per_page stays small on purpose: we only need
  // this ref's latest update, and other branches never crowd it. An empty result
  // means the ref has no update within the activity retention window (a
  // long-dormant PR) -- fail closed below rather than assume the head is fine.
  const res = await github.request('GET /repos/{owner}/{repo}/activity', {
    owner, repo: name, ref, direction: 'desc', per_page: 5,
  });
  const entries = (res.data || []).filter((e) => e.ref === ref);
  if (entries.length === 0) {
    throw new Error(`No activity for ${ref} in ${headRepo}; cannot confirm head, stopping`);
  }

  // Most recent update to the head ref (do not rely on sort order).
  const latest = entries.reduce((a, b) =>
    (new Date(b.timestamp).getTime() > new Date(a.timestamp).getTime() ? b : a));

  // The latest ref update must be the head we resolved; otherwise the branch is
  // mid-change or the activity log has not caught up -- stop rather than guess.
  if (latest.after !== headSha) {
    throw new Error(`Latest ${ref} update is ${latest.after} but PR head is ${headSha}; stopping`);
  }

  if (new Date(latest.timestamp).getTime() >= new Date(commentedAt).getTime()) {
    throw new Error(
      `${ref} was updated at ${latest.timestamp}, at or after the /strands `
      + `comment at ${commentedAt}; the head moved since the command was issued, stopping.`);
  }

  console.log(`Pinned head ${headSha} (head ref last updated ${latest.timestamp}, comment ${commentedAt})`);
  return { headSha, headRepo, headRef };
}

module.exports = pinAndVerify;
