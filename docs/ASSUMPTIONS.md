# Assumptions, behaviours and limitations

This document records what the proof of concept assumes, how it behaves where a reader might expect otherwise, and
what production would need. Every behaviour below is checked against the code (file:line) and a test. Decision IDs
(D1 to D28) refer to [PROGRESS.md](../PROGRESS.md).

## 1. Scope decisions (brief §2)

| Decision | Choice in this build |
|---|---|
| Form of "semantic" | A business glossary in YAML, with an owner, version, status and effective dates per concept. Not an ontology: no RDF, OWL, SKOS or graph database. |
| Catalog tools | None integrated. |
| Data | Synthetic only. The service returns metadata (definitions, sources, rules, relationships, governance fields), never rows about people. |
| Resolution | Deterministic code, no LLM. When more than one meaning fits, it returns candidates and a question; it never picks one. |
| Governance fields | Every concept has an owner team, semver version, status (`draft`/`approved`/`deprecated`) and effective dates. |
| Identity | API keys mapped to roles, as a stand-in (section 5). |
| Writes | None. The only POST routes (`/resolve`, `/evaluate`) compute and store nothing (`test_no_write_routes_exist`). |

## 2. Restricted visibility

**Policy: "existence".** A caller whose role is not in a concept's `allowed_roles` sees that the concept exists but
never its content:

- the list shows `{id, name, term, access: "restricted"}`;
- resolve returns only `restricted_count` and a warning;
- `GET`, `/relationships` and `/evaluate` return 403 with `required_role`;
- evaluate checks access before it looks at the facts, so a caller cannot learn a restricted rule's facts from a 422.

**How it is built.** There is one named setting, `RESTRICTED_VISIBILITY = "existence"` (`app/auth.py:23`), and one
function that decides visibility, `is_visible(caller, concept)` (`app/auth.py:35`). Every access decision calls that
function.

**Changing the policy is not a one-line flip.** The setting has a single allowed value and no code branches on it. A
different policy, such as **"hidden"** (behave as if the concept did not exist), is **not implemented**. It would need
edits in about four places, each at an `is_visible` call site:

1. **List** (`app/main.py:179`): leave the entry out instead of adding the `restricted` entry.
2. **The shared 404/403 check** (`app/main.py:136-140`, `visible_concept`): return 404 instead of 403. This one
   function serves GET, `/relationships` and `/evaluate`.
3. **Incoming relationships** (`app/main.py:207`): leave the entry out instead of adding the `restricted` entry.
4. **Resolver** (`app/resolver.py:92-102`): drop restricted matches before counting, so no `restricted_count`, no
   warning and no `restricted` status. This makes decision-table rows 2, 4, 6 and 8 behave differently.

The tests that pin the current policy would change with it.

## 3. Error semantics

- **Every error** is `{"error": {"code", "message", "details"}}` with a non-200 status. A route-wide test covers 33
  error cases; hostile-input tests show no input produces a 5xx.
- **Status codes:**
  - 400 `bad_request`: the body can't be parsed at all, e.g. not UTF-8, or JSON nested 100,000 levels deep.
  - 401 `unauthenticated`: the same body for a missing key, a wrong key, several key headers, an unknown path, a wrong
    method or an invalid body.
  - 403 `forbidden`.
  - 404 `not_found` (unknown id, draft or unknown route) and 404 `not_effective` (no version on that date).
  - 405 `method_not_allowed`.
  - 422 `validation_error`, `insufficient_context` (with `missing_facts`), `invalid_facts` and `not_evaluable`.
  - 500 `internal_error`: generic message, no stack trace.
- **Validation errors** keep only the location and message; the caller's input is never echoed back.
- **Resolve's four outcomes** (`resolved`, `ambiguous`, `not_found`, `restricted`) are answers, not errors, so they
  are HTTP 200, as the brief requires. The audit log still records `not_found` and `denied` for them.
- **A missing fact never produces true or false.** The evaluator first works out, from the rule itself, every fact it
  needs, following dependencies. If any is absent, it returns 422 before evaluating anything
  (`app/rules.py:95-114`). An absent key counts as missing; a key present with `null` is a value.

## 4. Behaviours a reader should know (each checked against the code)

| # | Behaviour | Code | Test |
|---|---|---|---|
| 1 | **A comparison against null is false.** Only `is_null`/`not_null` look at null, so `not (x == 3)` is **true** when `x` is null (SQL `WHERE` semantics). No seed rule negates a comparison on a nullable fact. | `app/rules.py:178` | `test_comparison_with_null_is_false[*]`, `test_not_over_null_comparison_is_true_documented_trap` |
| 2 | **Facts are supplied by the caller, not held by the service.** For example, `currently_eligible_member` reads `eligibility_status`; "eligible today" means "the status the caller says holds today", and that rule does not look at `requested_date`. | `catalog/concepts.yaml` (`currently_eligible_member`), `app/main.py:217` | `test_currently_eligible_member` |
| 3 | **Dependency fact names appear in `missing_facts`.** Evaluating `member_in_network` without coverage dates lists `coverage_end_date` and `coverage_start_date`, which belong to its dependency `active_member`. The dependency's definition is never returned. | `app/rules.py:100` | `test_evaluate_errors[member_in_network…]`, `test_evaluate_dependency_is_evaluated_but_not_exposed` |
| 4 | **A missing `as_of` means today's UTC date**, not server-local. Near midnight UTC this can be a day ahead of or behind the caller's local date; pass `as_of` when it matters. | `app/main.py:36` | `test_get_defaults_as_of_to_utc_today`, `test_resolve_defaults_as_of_to_utc_today` |
| 5 | **Drafts are invisible through the API for every role**, stewards included: never listed, 404 on GET and evaluate, never resolved (not even as a candidate). Steward review of drafts is a production concern. | `app/catalog.py:73`, `app/main.py:128`, `app/main.py:162` | `test_list_all_excludes_drafts_and_lists_each_version`, `test_draft_never_becomes_a_candidate`, eval case `10-draft` |
| 6 | **`/docs`, `/redoc` and `/openapi.json` are public**, like `/health`. They describe the API's shape; they contain no definitions, fact names or keys. | `app/main.py:30` | `test_public_paths_need_no_key`, `test_api_key_never_appears_in_logs_audit_errors_or_openapi` |
| 7 | **Rejected requests are audited with empty `params`.** A request that fails validation never reaches its route, and only routes fill in `params`. The route, status, error code and caller are still recorded. | `app/main.py:90`, `app/main.py:110` | `test_rejected_request_is_audited_with_empty_params` |
| 8 | **`/health` is audited** too, as is every other request, including 401s. | `app/main.py:91`, `app/main.py:110` | `test_outcome_and_error_code_per_status[…/health…]`, `test_one_line_per_request` |
| 9 | **A 500 is audited, but its cause is not logged anywhere.** The middleware catches the exception, returns the generic envelope and writes `outcome: error`, but records no traceback or exception text. That keeps internals out of logs, and it also means an operator cannot see why it failed. | `app/main.py:104` | `test_unhandled_exception_is_a_500_envelope_and_audited` |
| 10 | **The audit log has no rotation.** It is one append-only JSONL file; `GET /semantic/audit` reads the whole file to return the tail. It is safe across threads in one process, not across several worker processes. Fact values are never logged, only fact names. | `app/audit.py:45-51` | `test_concurrent_writes_stay_whole`, `test_evaluate_logs_fact_names_not_values` |
| 11 | **Candidate order is stable**: sorted by `context.system`, then `id`. For "member": analytics, claims, crm, customer_service, enrollment. | `app/resolver.py:34-41` | `test_member_without_context_is_ambiguous_with_exactly_five` |
| 12 | **The "hidden" visibility policy is not implemented** (section 2). | `app/auth.py:23` | (pinned by the existence-policy tests) |

**Also worth knowing:**
- **Matching is exact after normalisation:** casefolded, `_` and `-` read as spaces, whitespace collapsed. There is
  no fuzzy matching of candidates and no Unicode compatibility folding.
- **A context that matches none of the candidates** gives `ambiguous` with a warning, even when only one candidate
  exists. It never falls back to a guess.
- **The MCP `get_definition` tool makes two API calls**, so it writes two audit lines.
- **The MCP server logs each API request line** (method, URL, status) to its stderr. The URL never contains a key.
- **The MCP HTTP transport answers only `localhost` hosts** by default (the SDK's DNS-rebinding protection).
- **Requests uvicorn itself rejects** as malformed HTTP never reach the app, so they are not audited.

## 5. Identity: API keys versus the MCP authorization specification

**What exists.**
- Each role has a key in a YAML file outside version control.
- The API compares keys as SHA-256 digests with `hmac.compare_digest`, checking every key with no early exit.
- A key never appears in logs, audit lines, error bodies or the OpenAPI document, and a test checks this.
- Over Streamable HTTP the MCP server forwards the caller's own `X-API-Key` header to the API, and nothing else. Over
  stdio it uses `SEMANTIC_API_KEY`.

**What the current spec requires.** Read on 2026-09-20: MCP specification revision **2026-07-28**, "Authorization"
and "Security Best Practices".
- Authorization applies to HTTP transports. A stdio server "SHOULD NOT follow this specification, and instead
  retrieve credentials from the environment". The stdio path here already does that.
- For HTTP, the MCP server is an OAuth 2.1 resource server:
  - It MUST publish OAuth 2.0 Protected Resource Metadata (RFC 9728).
  - It answers unauthenticated requests with 401 and a `WWW-Authenticate` header pointing at that metadata.
- Clients get tokens from an authorization server using PKCE:
  - they find it via RFC 8414 or OpenID Connect discovery;
  - they register with Client ID Metadata Documents, pre-registration, or the now-deprecated Dynamic Client
    Registration;
  - they MUST send a `resource` parameter (RFC 8707) naming the MCP server.
- The server MUST validate that each token was issued for it (the audience check).
- Missing scope is answered with 403 `insufficient_scope`, which lets clients step up their scopes.
- **Token passthrough is forbidden.** The server "MUST NOT accept or transit any other tokens", and the Security
  Best Practices page names forwarding a client's token to a downstream API as an anti-pattern (confused deputy).

**The gap.**
1. **No OAuth at all.** There is no authorization server, no Protected Resource Metadata endpoint, no
   `WWW-Authenticate` challenge, no token validation and no scopes. The MCP HTTP endpoint accepts any request and
   leaves authentication to the API.
2. **Forwarding the key is the pattern the spec forbids for tokens.** With an opaque demo key this is equivalent to
   the caller using the API directly, and the API still authorizes every call. Under OAuth, though, the MCP server
   must validate a token issued *for itself*, then call the API with a separate credential: its own, or a token
   obtained on the user's behalf through token exchange. The user's identity must still reach the API, for role
   checks and for the audit line.
3. **Roles would come from token claims or scopes**, e.g. a scope per role or a restricted-concepts scope, not from
   a key file.

## 6. Other limitations

- **`rule.text` and `rule.expression` are kept in sync by hand.** The text is for people and the expression is what
  runs. Nothing checks that they say the same thing; that is a stewardship responsibility.
- **The catalog is an in-memory copy of a YAML file.** Changes mean editing the file and restarting. Startup
  validation reports every problem in one pass, and the app refuses to start on any. There is no persistence, history
  or concurrent editing.
- **`currently_eligible_member`** depends on a status snapshot supplied by the caller (behaviour 2).
- **Seed data and demo population are synthetic.** The demo's population comes from a seeded generator in
  `tests/fixtures/`, is evaluated with the rule library, and is never reachable through the API.

## 7. What I would do differently in production

1. **Identity:** OAuth 2.1 per the MCP authorization spec (section 5). That means Protected Resource Metadata on the
   MCP server, audience-checked tokens, scopes mapped to roles, and no passthrough: the MCP server gets its own
   credential for the API, and user identity reaches the API by token exchange. Keys and the key file go away.
2. **Catalog integration:** read concepts from the organisation's catalog or metadata store instead of a YAML file,
   keeping the startup validation as a publish-time check.
3. **Persistence and audit:**
   - store concepts and their versions in a database;
   - send audit events to a central, append-only store with retention and rotation;
   - log 500 causes to a restricted operator log.
4. **Stewardship workflow:** draft, review, approve and deprecate, with steward-only visibility of drafts and a
   recorded approver.
5. **Rule/text sync:** generate the human-readable text from the expression, or check the two against each other in
   CI, so they cannot drift.
6. **Caching:** cache resolved definitions per (term, context, as_of, role), invalidated when the catalog changes,
   and set HTTP cache headers on definitions.
7. **Visibility policy:** make the restricted policy a real switch if both policies are needed (section 2).
