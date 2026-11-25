"""Settings-bound GitHub tool and dispatch; helper imports remain compatible here."""

from __future__ import annotations

import json
import urllib.error
from typing import TYPE_CHECKING, Any

from .github_client import GITHUB_GRAPHQL_URL as GITHUB_GRAPHQL_URL
from .github_client import GITHUB_REST_URL as GITHUB_REST_URL
from .github_client import _get_token as _get_token
from .github_client import _graphql as _graphql
from .github_client import _request as _request
from .github_client import _rest_call as _rest_call
from .github_client import _rest_get as _rest_get
from .github_client import count_external_writes as count_external_writes
from .github_client import resolve_node_owner as resolve_node_owner
from .github_client import resolve_self_login as resolve_self_login
from .github_guardrails import NODE_ID_PATTERN as NODE_ID_PATTERN
from .github_guardrails import REPO_SCOPED_PREFIXES as REPO_SCOPED_PREFIXES
from .github_guardrails import _target_allowed as _target_allowed
from .github_guardrails import enforce_throttle as enforce_throttle
from .github_guardrails import extract_node_ids_from_query as extract_node_ids_from_query
from .github_guardrails import extract_node_ids_from_variables as extract_node_ids_from_variables
from .github_guardrails import extract_owner_from_variables as extract_owner_from_variables
from .github_guardrails import extract_target_from_variables as extract_target_from_variables
from .github_guardrails import invalidate_throttle_cache as invalidate_throttle_cache
from .github_guardrails import is_mutation_query as is_mutation_query
from .github_guardrails import parse_rest_path as parse_rest_path
from .github_guardrails import validate_owner as validate_owner
from .github_guardrails import validate_rest_path as validate_rest_path

if TYPE_CHECKING:
    from strandly.core.config import GitHubSettings


def _result(status: str, text: str) -> dict[str, Any]:
    return {"status": status, "content": [{"text": text}]}


_REST_ACCEPT_ALLOWED = frozenset(
    {
        "application/vnd.github+json",
        "application/vnd.github.v3.diff",
        "application/vnd.github.diff",
        "application/vnd.github.v3.patch",
        "application/vnd.github.patch",
        "application/vnd.github.raw",
        "application/vnd.github.v3.raw",
    }
)


def _rest_branch(
    *,
    gh: GitHubSettings,
    allowed_owners: set[str],
    path: str,
    method: str,
    body: dict[str, Any] | None,
    accept: str,
    use_pat_token: bool,
) -> dict[str, Any]:
    """The REST half of ``use_github`` — same guardrail order as the GraphQL half.

    Split out of the tool closure so it is directly unit-testable (the tool body itself is wrapped by
    the ``@tool`` decorator).
    """
    token = _get_token(gh, use_pat_token)
    if not token:
        return _result("error", f"No GitHub token found. Set one of: {', '.join(gh.token_env)}.")

    if accept and accept not in _REST_ACCEPT_ALLOWED:
        return _result(
            "error",
            f"🛑 Blocked: Accept '{accept}' is not allowed "
            f"({', '.join(sorted(_REST_ACCEPT_ALLOWED))}).",
        )

    verb = (method or "GET").upper()
    # Guardrails 1–3 equivalent: path shape + resource allow-list + owner from the path.
    path_error, target = validate_rest_path(path, verb, allowed_owners=allowed_owners)
    if path_error:
        return _result("error", f"🛑 {path_error}")

    # Guardrail 4: the daily external-write throttle — same policy as a GraphQL mutation, since a
    # non-GET REST call is exactly as much a write. A target-less write (own account) still throttles
    # under its family name so it can't be used as an unmetered side channel.
    if verb != "GET":
        _, family = parse_rest_path(path)
        allowed, msg = enforce_throttle(target or family, gh=gh, token=token)
        if not allowed:
            return _result("error", f"🛑 SAFETY GUARDRAIL — {msg}")

    try:
        response = _rest_call(verb, path, token, body=body, accept=accept)
    except urllib.error.HTTPError as e:
        return _result("error", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        return _result("error", f"Request error: {e.reason}")
    except Exception as e:  # noqa: BLE001
        return _result("error", f"GitHub REST call failed: {e}")

    if isinstance(response, str):
        return _result("success", response)
    return _result("success", json.dumps(response, indent=2))


def make_use_github(gh: GitHubSettings) -> Any:
    """Return a ``use_github`` tool bound to the harness's GitHub guardrail settings.

    Built and injected by ``build_agent`` (settings-aware, like ``subagent``), not a plain builtin.
    """
    from strands import tool

    allowed_owners = set(gh.allowed_owners)

    @tool(name="use_github")
    def use_github(
        query_type: str,
        query: str,
        label: str,
        variables: dict[str, Any] | None = None,
        use_pat_token: bool = False,
        rest_path: str = "",
        rest_method: str = "GET",
        rest_body: dict[str, Any] | None = None,
        rest_accept: str = "",
    ) -> dict[str, Any]:
        """Act on GitHub — GraphQL v4 by default, or REST v3 for what GraphQL can't express.

        **GraphQL (default).** Pass ``query_type``/``query``/``variables``. Use it for
        repository/issue/PR/project data and, with a write-scoped token, mutations (commenting,
        reviewing, labelling, merging).

        **REST (set ``rest_path``).** GraphQL genuinely cannot do some things; reach for REST only
        for those:

        - a PR's **patch text** — ``/repos/{o}/{r}/pulls/{n}/files``, or the PR itself with
          ``rest_accept="application/vnd.github.v3.diff"`` (GraphQL's ``files`` has paths but no
          ``patch``)
        - **compare two refs** — ``/repos/{o}/{r}/compare/{base}...{head}``
        - **code search** — ``/search/code?q=…`` (GraphQL ``search`` has no CODE type)
        - **Actions** runs / jobs / logs — ``/repos/{o}/{r}/actions/runs``
        - **recursive tree** — ``/repos/{o}/{r}/git/trees/{ref}?recursive=1``
        - **raw or binary file bodies** — ``/repos/{o}/{r}/contents/{path}`` with
          ``rest_accept="application/vnd.github.raw"`` (a GraphQL blob's ``text`` is null for binary
          and truncated when large)
        - **gists**, and **collaborators/invitations** (no GraphQL mutation exists)

        **For a working tree, prefer git over either.** ``git clone`` / ``fetch`` / ``diff`` / ``log``
        / ``blame`` in your sandbox is the right way to read code and history at scale — it's faster,
        gives full context, and needs no API budget. Use REST for diffs when you want the patch
        *without* a clone.

        The same guardrails cover both surfaces: an owner allow-list, node-id → owner resolution for
        GraphQL mutations, an allow-list of mutation fields whose target must resolve to an allowed
        repository (strict mode), path/resource allow-listing for REST, and a daily external-write
        throttle. A blocked call returns ``status="error"`` with the reason — **do not try to work
        around a guardrail** (in particular, never read a credential out of the sandbox to call the
        API directly; that bypasses every check here and the sandbox's own token deliberately cannot
        do these operations).

        Args:
            query_type: "query" or "mutation". Ignored when ``rest_path`` is set.
            query: The GraphQL query/mutation string. Ignored when ``rest_path`` is set.
            label: Short human-readable description of the operation (for logs/approval).
            variables: Optional variables for the query.
            use_pat_token: Prefer the PAT_TOKEN env var over GITHUB_TOKEN (use sparingly — a PAT
                can re-trigger workflows).
            rest_path: A bare REST v3 path to call instead of GraphQL, e.g.
                ``"/repos/strands-agents/harness-sdk/pulls/42/files"``. Query strings are allowed.
            rest_method: REST verb — GET (default), POST, PATCH, PUT, DELETE.
            rest_body: JSON body for a non-GET REST call.
            rest_accept: Optional ``Accept`` override, e.g.
                ``"application/vnd.github.v3.diff"`` for a unified diff or
                ``"application/vnd.github.raw"`` for a raw file body.

        Returns:
            ``{"status": "success"|"error", "content": [{"text": <formatted result or error>}]}``.
        """
        if rest_path:
            return _rest_branch(
                gh=gh,
                allowed_owners=allowed_owners,
                path=rest_path,
                method=rest_method,
                body=rest_body,
                accept=rest_accept,
                use_pat_token=use_pat_token,
            )
        vars_ = variables or {}
        is_mutative = is_mutation_query(query, query_type)
        token = _get_token(gh, use_pat_token)
        if not token:
            return _result(
                "error",
                f"No GitHub token found. Set one of: {', '.join(gh.token_env)}.",
            )

        # Guardrail 1–3: owner allow-list + node-id resolution + strict mode.
        owner_error, resolved_owner = validate_owner(
            vars_,
            allowed_owners=allowed_owners,
            is_mutative=is_mutative,
            strict=gh.strict_mutations,
            token=token,
            query=query,
        )
        if owner_error:
            return _result("error", f"🛑 {owner_error}")

        # Guardrail 4: daily external-write throttle (mutations only).
        if is_mutative:
            target = resolved_owner or extract_owner_from_variables(vars_)
            allowed, msg = enforce_throttle(target, gh=gh, token=token)
            if not allowed:
                return _result("error", f"🛑 SAFETY GUARDRAIL — {msg}")

        # Execute.
        try:
            response = _graphql(query, vars_, token)
        except urllib.error.HTTPError as e:
            return _result("error", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        except urllib.error.URLError as e:
            return _result("error", f"Request error: {e.reason}")
        except Exception as e:  # noqa: BLE001
            return _result("error", f"GitHub call failed: {e}")

        if "errors" in response:
            return _result(
                "error",
                "GraphQL errors:\n" + json.dumps(response["errors"], indent=2),
            )
        return _result("success", json.dumps(response.get("data", {}), indent=2))

    return use_github
