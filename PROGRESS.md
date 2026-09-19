# PROGRESS

Running log for the Ryan-MCP build. The spec is `BUILD_BRIEF.md`; this file records status, decisions
(with the alternative rejected), deviations and open questions.

## Status

| Milestone | Status | Tag |
|---|---|---|
| M0 Scaffold | done | `m0-done` |
| M1 Models, catalog, validation | done | `m1-done` |
| M2 Rule evaluator | done | `m2-done` |
| M3 API foundation | done | `m3-done` |
| M4 Resolver + /resolve | done | `m4-done` |
| M5 MCP server | done | `m5-done` |
| M6 Tests, eval, demo | not started | |
| M7 Docs | not started | |

## DP1 (approved 2026-09-19, developer's "go" with all four default answers)

1. Python 3.11 installed with Homebrew on the dev machine (only 3.12/3.14 were present).
2. A top-level fact dictionary in `concepts.yaml`, and a check that `depends_on` equals expression `{concept:}` refs.
3. A comparison against `null` is `false` (SQL `WHERE` semantics); `eq`/`ne` with a `null` literal is rejected at load.
4. Deprecated = `covered_life`; draft = `engaged_member` (term `member`); `reporting_month_member` v1 "first day of month" → v2 "any portion".

## DP2 (approved 2026-09-20, with the developer's changes)

1. Only the `existence` restricted-visibility policy is built. `is_visible(caller, concept)` in `app/auth.py` is the
   only access predicate, and `RESTRICTED_VISIBILITY` is the single named setting.
2. `GET /semantic/audit` is built: steward only, fixed file path (no path parameter), `limit` bounded 1–1000, and its
   own access is audited like any request.
3. A context that matches none of the candidates gives `ambiguous` plus a context warning (decision-table row 7). With
   one remaining candidate the clarifying question names that meaning and asks whether it is the one needed.
4. Drafts never appear through the API for any role: not listed, 404 on GET and evaluate, never resolved.
5. Default `as_of` is **today's UTC date**, not server-local.
6. The audit middleware never reads the request body. Routes put the known fields on `request.state.audit_params`
   (and resolve sets `request.state.audit_outcome`); the middleware writes the line after the response.
7. Evaluate returns `concept_id, result, rule_text, source, version, status, requested_date, warnings`.
8. `GET /concepts/{id}/relationships` applies the same 404/403 checks to the concept itself as `GET /concepts/{id}`.
9. `term` and `system` query parameters on `GET /concepts` are length-bounded.

## Documented behaviours and limitations (feed docs/ASSUMPTIONS.md)

- **Null comparisons are false.** Only `is_null`/`not_null` observe null, so `not (x == 3)` is **true** when `x` is null
  (SQL `WHERE` semantics, D8). No seed rule negates a comparison on a nullable fact.
- **Facts are caller-supplied.** The service holds no member data. Facts such as `currently_eligible_member`'s
  `eligibility_status` come from the caller, who is responsible for them being current; "eligible today" therefore
  means "the status the caller says holds today", and that concept does not look at `requested_date`.
- **Restricted visibility is `existence` only.** Callers without the role see that a restricted concept exists (list
  entry, `restricted_count` in resolve, 403 on GET/evaluate/relationships) but never its content. A `hidden` policy
  (behave as if the concept did not exist) is a production option, not built.
- **Drafts are invisible to every role.** Steward review of drafts through the API is a production concern.
- **Audit log**: one process only (the lock does not coordinate several uvicorn workers); `GET /semantic/audit` reads
  the whole file to take its tail, fine for a demo, not for a large log. Fact values are never logged.
- **Public docs**: `/docs`, `/redoc` and `/openapi.json` need no key; they describe the API, not the catalog.
- **Requests the HTTP server rejects are not audited**: uvicorn answers malformed requests (e.g. a bare LF inside a
  header) with its own 400 before the app sees them.
- **Term matching is exact after normalisation** (casefold, `_`/`-` as spaces, whitespace collapsed). No Unicode
  compatibility folding: full-width `ＭＥＭＢＥＲ` does not match `member` (`not_found`, no suggestion).
- **Resolve's four outcomes are HTTP 200** (brief §6), including `not_found` and `restricted`; they are answers.
  The audit line still records `not_found` / `denied` for them.
- **MCP key forwarding**: over Streamable HTTP the MCP server forwards only the caller's own `X-API-Key` (exactly one;
  none or several → nothing forwarded → 401); over stdio it uses `SEMANTIC_API_KEY`. It never falls back to its own key
  on HTTP. Every MCP call is audited with `via: "mcp"`.
- **`get_definition` is two API calls** (`GET /concepts/{id}` and `/relationships`), so it writes two audit lines.
- **MCP HTTP is localhost-only by default**: the SDK's DNS-rebinding protection answers only localhost `Host` headers;
  serving it remotely needs `transport_security` settings, which are not configured here.
- **MCP server logs**: `httpx2` logs each API request line (method, URL, status) at INFO to the MCP server's stderr.
  URLs carry concept ids and `as_of`, never keys (scanned in `test_keys_never_appear_in_mcp_server_output_or_results`).
- **Default `as_of` is the UTC date.**
 Near midnight UTC it can be a day ahead of or behind the caller's local date;
  pass `as_of` explicitly when it matters.

## Decisions

### D1. Fact dictionary (`facts:` at the top of `concepts.yaml`) — deviation from brief §4a
Each fact declares `type` (`date|integer|string|boolean`), `nullable` (default false) and optionally `not_before: <fact>`.
- Why: without declared types the evaluator cannot reject a bad date string, the loader cannot type-check literals,
  and nothing says which facts may be null. `not_before` is the cheap mechanism for `invalid_facts`
  (coverage end before start), about five lines of evaluator code.
- Rejected: inferring types from operands (fails on `claim_count_to_date >= 1`, silently wrong on strings that look
  like dates); a per-concept fact list (the same fact would be typed in several places).

### D2. `{fact: X}` operand — extension of the brief §5 shape
`reporting_month_member` compares coverage dates with the reporting-month facts, not with `requested_date`.
Operands may be a literal, `{ref: requested_date}` or `{fact: X}`. Operand facts count as required facts.
- Rejected: extra `ref` names such as `reporting_month_start` (they are caller facts, not request parameters).

### D3. Keying and references
Versions are stored as `dict[id, list[Concept]]` sorted by `effective_from`; `(id, version)` is unique.
All references (`{concept:}`, `superseded_by`, `depends_on`) name an **id**; the version is chosen at call time as the
non-draft version whose `[effective_from, effective_to]` contains the date. `effective_to` is **inclusive**, matching
`coverage_end_date >= requested_date`. Relationship targets may be an id or a term.
- Rejected: version-pinned references (`active_member@1.0.0`), which would force every dependant to be re-versioned
  whenever a dependency changes.

### D4. Validation collects everything in one pass
Each concept is parsed separately; every Pydantic error becomes one issue naming the concept id, version and field
path. Cross-concept checks then run over the parsed concepts, using the raw ids of unparsable concepts for existence
checks so that one schema error does not cascade into false "target does not exist" errors. One `CatalogError`
lists every issue.
- Rejected: parsing the whole file as one Pydantic model (errors are not grouped by concept id; cross-checks cannot run
  if any concept is malformed).

### D5. `owner` required on every concept (schema level)
§2 says every concept has an owner, so it is required by the schema rather than only for `approved`. `rule` and
`authoritative_source` are optional in the schema and required for `approved` by a validation check (§4a).

### D6. `depends_on` must equal the expression's `{concept:}` refs
The dependency is stated twice (relationship and expression); the check stops the two from drifting.
- Rejected: deriving `depends_on` from the expression (brief lists relationships as authored data).

### D7. Evaluator: required facts are derived statically
`required_facts` walks the expression tree (following `{concept:}` into the dependency's version effective on
`requested_date`) without evaluating anything. Missing = required keys **absent** from `facts`
(`key not in facts`); a present `null` is a value. Order: missing → type coercion (all errors collected) →
`not_before` → evaluate. Short-circuiting is safe because evaluation only starts once every required fact is present
and valid.
- Rejected: collecting facts during evaluation (short-circuit in `all`/`any`/`not` would hide missing facts and return
  a boolean — prototype mistake 3).

### D8. Null semantics
Only `is_null`/`not_null` observe null. Any other comparison with a null operand is `false`. Known trap:
`not` over a null comparison is `true`; the seed rules do not do this (documented limitation).

### D9. Evaluator is a pure library that raises typed exceptions
`InsufficientContext(missing_facts)`, `InvalidFacts(errors)`, `ConceptNotFound`, `ConceptNotEffective`.
HTTP status mapping is M3's job.

### D10. Overlap check covers all non-draft versions (M1)
Brief §4a asks that no two *approved* versions of one id overlap. The check is applied to approved **and** deprecated
versions, because either can be selected by date; two overlapping candidates would force a silent choice.
Drafts may overlap (a draft v3 can be prepared while v2 is live).
- Rejected: approved-only (a deprecated v1 overlapping an approved v2 would make `effective()` order-dependent).

### D11. Structural vs cross-object validation split (M1)
`app/models.py` checks shapes only (Pydantic, `extra="forbid"`); every check needing more than one object
(references, overlaps, cycles, expression-vs-fact-dictionary types, `not_before`) lives in `app/catalog.py`, so a
single bad concept never stops the other checks from running. Expression node type is chosen by a callable
discriminator on the node's key, so errors name the node kind instead of listing every union member.
Type rules enforced at load: ordering ops only on date/integer; `in` only on string/integer with a literal list;
boolean facts only `eq`/`ne`; `is_null`/`not_null` only on nullable facts (otherwise the test is constant);
`{ref: requested_date}` only against date facts; `{fact:}` operands must have the same type.

### D12. `load_catalog` uses `yaml.safe_load`
No Python objects can be constructed from YAML tags (tested with a `!!python/object/apply` payload).

### D13. Evaluator error for a non-draft concept without a rule (M2)
§4a only requires a rule on `approved` concepts, so a `deprecated` concept may have none. Evaluating one raises
`NoRule` instead of a boolean. `ConceptNotFound` covers unknown ids **and** draft-only ids (brief §6: draft → 404).
- Rejected: requiring a rule on deprecated concepts (stricter than the brief, for no demo benefit).

### D14. Fact coercion is strict (M2)
Dates must be `YYYY-MM-DD` with ASCII digits (`date.fromisoformat` alone would also accept `20260101` and
`2026-W01-1` on 3.11); integers reject `bool` and `3.0`; booleans reject `"true"`/`1`; strings reject numbers.
Only facts the rule needs are checked, and extra facts are ignored, so a caller may send one superset of facts.

### D15. API keys (M3)
YAML file at `SEMANTIC_API_KEYS_FILE` (default `config/api_keys.yaml`, git-ignored; only the `.example` file is
committed). Startup fails listing every problem (missing file, bad YAML, empty list, unknown role, key < 16 chars,
duplicate label or key); messages name the label or index, never the key, and YAML errors report the line number only
(PyYAML's own message quotes the offending line, which may contain a key). Comparison: SHA-256 of the presented key
vs each stored digest with `hmac.compare_digest`, looping over **all** keys without an early exit. Hashing gives
equal-length inputs and avoids the `TypeError` `compare_digest` raises on non-ASCII `str`.
- Rejected: keys inline in an env var (awkward on PowerShell, easy to leak in shell history); plain `==` (timing).

### D16. Auth and audit in one middleware, deny by default (M3)
Every path except `/health`, `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` needs a key, checked
**before routing**, so an unauthenticated caller gets the same 401 whether the path exists, the body is invalid or
the method is wrong. The middleware writes one audit line per request after the response, from `request.state`
(caller, `audit_params`, `audit_outcome`, `error_code`) and `scope["route"].path` (the route template, set by
FastAPI). **It never reads the request body**: routes put the allow-listed fields on `request.state.audit_params`,
error handlers put the code on `request.state.error_code`. A request carrying more than one `X-API-Key` header is
401 (found in the M4 adversarial pass: the app read the first while a proxy might read the last). An exception escaping a route is caught there, audited as
`error`, and returned as the 500 envelope.
- Rejected: a FastAPI dependency for auth (validation errors could be reported before the 401; unknown paths would
  404 without a key); reading the body in middleware (consumes the stream, and logs whatever the caller sent).

### D17. Audit line (M3)
`ts` (UTC), `key_label`, `role`, `via`, `method`, `endpoint`, `params`, `status_code`, `error_code`, `outcome`.
`via` is `"mcp"` only when `X-Via` is exactly `mcp`, else `"api"`; the header text is never copied. Params are an
allow-list; for evaluate only **fact names** are logged, never values (no person-level data in the log). Strings are
cut to 200 chars and containers to 50 items. `json.dumps(ensure_ascii=True)` escapes newlines and separators, so one
request is one line. `threading.Lock` + append mode: safe across threads in one process.

### D18. Error contract (M3)
Envelope everywhere; handlers for `ApiError`, `RequestValidationError` (only `loc` and `msg` kept: `input`/`ctx`
are dropped so caller input is never echoed), Starlette `HTTPException` (404/405/400) and `Exception` (500, generic).
Responses are rendered with `ensure_ascii=True` as defence in depth: Pydantic already rejects a lone surrogate in a
`str` field (422), but untyped values (fact values) are not validated as text. Measured in M4: a body that is not
UTF-8, or JSON nested 100,000 deep, gets FastAPI's own parse error, enveloped as 400 `bad_request`.
Order for concept routes: unknown/draft → 404 `not_found`; no version on the date → 404 `not_effective`; role not
allowed → 403 `forbidden`. Evaluate checks access **before** looking at facts, so a restricted concept's required
facts cannot be probed. A dependency that is not effective (or has no rule) during evaluate is 422 `not_evaluable`.

### D19. Request dates (M3)
`as_of` and `requested_date` use one `IsoDate` type with the same rule as fact dates (D14): `YYYY-MM-DD`, ASCII
digits. Pydantic's lax `date` would accept integers as Unix timestamps. Default `as_of` is today's **UTC** date.

### D20. API surface details (M3)
- `GET /concepts/{id}/relationships` accepts the same optional `as_of` as `GET /concepts/{id}` (not in brief §6) so
  both pick the same version. Incoming relationships are derived from other concepts effective on that date; a
  restricted neighbour appears as `{source, type, access: "restricted"}` without its description.
- `GET /concepts` lists one entry per non-draft version, ordered by `(id, effective_from)`; a restricted concept
  appears once as `{id, name, term, access: "restricted"}`. `status` accepts `approved|deprecated` only. `term` is
  bounded to 200 characters, `system` to 64. Filters apply to the underlying concept, so filtering by `system` can
  confirm a restricted concept's system: existence-level metadata, allowed by the `existence` policy.
- `concept_id` path parameters are bounded to 100 characters; longer is a 422.
- Evaluate returns `concept_id, result, rule_text, source, version, status, requested_date, warnings`. A dependency
  without an effective version or rule is 422 `not_evaluable`; the message names the dependency id (ids are
  existence-level). Missing-fact lists may include a dependency's fact names (brief: dependencies are evaluated
  internally, their definitions are not exposed).
- `GET /semantic/audit`: steward only, fixed file (no path parameter), `limit` 1–1000 (default 50).
- The only non-GET routes are `POST /resolve` and `POST /evaluate`; both compute and store nothing.

### D21. Resolver (M4)
Pure function `resolve(catalog, term, as_of, caller, system=None, domain=None) -> Resolution` in `app/resolver.py`;
no HTTP, no clock, no I/O, so the eval runner can call it directly.
1. Normalise (`normalize_term`: casefold, `_`/`-` → space, collapse whitespace); exact match against the normalised
   id, name, term and aliases. No fuzzy matching of candidates.
2. M = matching concepts, one per id: the non-draft version effective on `as_of`. Drafts and out-of-date versions
   never count.
3. Context narrows M on `system` and/or `domain` (normalised; both must match when both are given). If it matches
   none, M is kept whole and a warning is added; the outcome cannot be `resolved` (rows 7/8).
4. V = visible to the caller (`is_visible`), R = the rest, carried **only as a count**. `Resolution` has no field
   that could hold a restricted concept.
5. |V| = 0 → `restricted`; |V| = 1 (context not contradicted) → `resolved`; otherwise `ambiguous`; |M| = 0 →
   `not_found` with up to 3 `difflib` suggestions (cutoff 0.6) drawn only from concepts the caller can see.
Candidate order: `(context.system, id)`, alphabetical. The clarifying question lists the candidates' systems (or ids
when systems repeat); with one candidate left after a contradicting context it names that meaning and asks whether it
is the one needed.
- Rejected: substring or fuzzy candidate matching (would pull member_in_network into "member" and break the
  5-candidate contract); ranking candidates (the brief forbids choosing).

### D22. Resolve request bounds (M4)
`term`: 1–200 chars and non-empty after normalisation (`"___"` or `"   "` is a 422, not a silent not_found); the
same rule applies to `context.system`/`context.domain` (1–64 chars). They are free text: an unknown system such as
`finance` must reach the resolver and produce the row-7 warning, not a 422. Unknown body fields are a 422.

### D23. MCP SDK facts, from docs and installed source (M5)
Researched 2026-09-20 from the package README (`mcp-2.2.0.dist-info/METADATA`), the v2 docs at
py.sdk.modelcontextprotocol.io (servers/structured-output, servers/handling-errors, handlers/context, run/asgi,
client/transports) and the installed source; not from memory. v2 is a breaking rework of v1 (`FastMCP` → `MCPServer`).
- **Version**: `mcp==2.2.0` (current stable, v2 line, supports the 2026-07-28 spec). Requires `httpx2>=2.5.0`,
  `starlette`, `uvicorn`, `pydantic>=2.12`, `sse-starlette`; installs cleanly next to our pins (`pip check`: ok).
- **Server**: `from mcp.server import MCPServer`; `MCPServer(name, instructions=...)`; tools via
  `@mcp.tool(description=...)` (`server/mcpserver/server.py:660`).
- **Stdio**: `mcp.run("stdio")` (default transport) (`server.py:400-420`).
- **Streamable HTTP**: `mcp.run("streamable-http", host=..., port=..., streamable_http_path="/mcp")`; default
  `127.0.0.1:8000/mcp`. DNS-rebinding protection answers only localhost hosts unless `transport_security=` is set.
- **Input schema**: generated from the parameter type hints; `Annotated[str, Field(description=...)]` puts a
  description on each property.
- **Structured output**: from the return annotation. `dict[str, ...]` stays unwrapped; scalars become
  `{"result": ...}`. A tool may return `CallToolResult` itself; with `Annotated[CallToolResult, T]` the result is
  validated against `T` **only when `is_error` is false** (`utilities/func_metadata.py:204-207`), and the SDK client
  also validates structured content only for non-error results (`client/session.py:1101`).
- **Errors**: `ToolError` (from `mcp.server.mcpserver.exceptions`) → `is_error=True` with the message as text and no
  structured content; any other exception → generic "Error executing tool" and a server traceback; `MCPError` →
  JSON-RPC protocol error.
- **Request headers inside a tool**: add a parameter typed `Context` (`from mcp.server.mcpserver import Context`);
  `ctx.headers` is the HTTP request's headers on Streamable HTTP and `None` on stdio (`server/mcpserver/context.py:282`).
  The docs warn: "Headers are client-supplied input ... never an identity"; here the header is only *forwarded*, and
  the API decides identity.
- **Client**: `from mcp import Client`; `Client("http://.../mcp")`, `Client(StdioServerParameters(command, args, env))`
  or `Client(mcp_server_instance)` (in-process). Custom HTTP headers go on an `httpx2.AsyncClient` passed to
  `mcp.client.streamable_http.streamable_http_client(url, http_client=...)`. A stdio child inherits only an
  allow-list of environment variables (`client/stdio.py:40`), so keys must be passed in `env=` explicitly.

### D24. HTTP client: `httpx2`, and `httpx` is dropped (M5)
Evidence, not memory: Starlette 1.6's `testclient.py:33-50` imports `httpx2` first and falls back to `httpx` with
"Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead."; `mcp==2.2.0` itself requires
`httpx2>=2.5.0`; `httpx2` (Pydantic-maintained continuation of HTTPX) has the same API. So `mcp_server/` uses
`httpx2`, pinned in `requirements.txt` because it is imported directly, and `httpx` leaves `requirements-dev.txt`
(tests only use Starlette's `TestClient`). No package is added that the SDK does not already bring.
- Rejected: keeping `httpx` (a second HTTP client for the same job, and a deprecated path in Starlette).

### D25. MCP server: a thin HTTP wrapper (M5)
`mcp_server/server.py` never imports `app`; it calls the API over HTTP at `SEMANTIC_API_URL` (default
`http://127.0.0.1:8000`) and sends `X-Via: mcp` on every call.
- **Key forwarding**: on Streamable HTTP the key is the caller's own `X-API-Key` header and nothing else; if the
  caller sent none, none is forwarded and the API answers 401. It never falls back to a server-side key, which would
  give every unauthenticated HTTP caller that identity. On stdio (`ctx.headers is None`, one local user) the key
  comes from `SEMANTIC_API_KEY`.
- **Errors**: an API error envelope becomes `is_error=True` with `structured_content = {"http_status", "error"}`
  (the API's own envelope) and the same JSON as text; an unreachable API becomes `is_error=True` with code
  `api_unreachable`. No stack traces reach the model.
- **Results**: the API's JSON is passed through unchanged (`authoritative_source`, `rule.text`, ... keep their API
  names). `get_definition` makes two API calls (`GET /concepts/{id}` then `/relationships`, both audited) and adds
  `incoming_relationships` to the concept.
- Rejected: mounting the MCP server inside the FastAPI app (couples the wrapper to the service, and the brief wants
  the MCP layer to be a client of the API); `ToolError` for API errors (text only, loses the structured envelope).

### D26. Eval runner (M6)
`evals/ambiguous_questions.yaml` holds 13 cases (the brief's ten, with case 7 split by role, case 9 split by date,
plus a deprecated-term case). Each case: `id`, `question`, `term`, `role`, `context`, `as_of` (pinned, so results do
not change with the calendar), `expected_status`, `expected_concepts` (resolved → the one id; ambiguous → candidate
ids in order), optional `expected_version`, `expected_suggestions`, `expected_restricted_count`,
`expected_warning` (substring), `rationale`, and `mcp: true` for the three core cases (ambiguous "member",
"member" narrowed to enrollment, restricted follow-up as analyst).
`python -m evals.run` calls the pure resolver for every case, then runs the `mcp: true` cases through the real MCP
tool path: it starts the API in-process on a free local port (uvicorn in a thread) with a throwaway key file of
random keys and a throwaway audit file, and calls `resolve_term` through an in-process MCP client, which reaches the
API over HTTP. It prints a pass/fail table and exits 1 on any failure.
- Rejected: requiring a running API and `config/api_keys.yaml` for the eval (the eval must run from a fresh clone);
  running every case through MCP (slower, and the resolver is the unit under test; three cases prove the path).

### D27. Demo script and its fixture (M6)
`tests/fixtures/synthetic_people.py` generates 50 synthetic people with `random.Random(seed)` (default seed
20260920); every person carries every fact the five `member` rules need, and `coverage_end_date` is either null or
not before `coverage_start_date`. `python -m scripts.demo_conflict` evaluates the five `member` definitions with
`app.rules.evaluate` (the library, never the API) on 2026-09-15 for the September 2026 reporting month, prints the
five counts side by side, then three people who are a member under one definition and not another, each with a
one-line reason. Nothing in the API can reach the fixture (`test_no_write_routes_exist` pins the route list).
- Rejected: a committed JSON population (the brief asks for a seeded generator; the generator is the reviewable
  artefact); evaluating through the API (the API must never take a population).

## Deviations from the brief
- D1 fact dictionary and D2 `{fact:}` operand (both approved at DP1).
- D6 extra validation rule (approved at DP1).
- D10: overlap check also covers deprecated versions (stricter than §4a).
- D13: a deprecated concept may lack a rule; evaluating it is `not_evaluable`.
- D20: `as_of` added to `GET /concepts/{id}/relationships`; `status` filter excludes `draft`.
- D16: `/health` requests are audited too ("one line per request").
- D24: `httpx2` instead of the brief's `httpx` (evidence in D24).
- D25: MCP results keep the API's field names (`authoritative_source`, `rule.text`) instead of renaming them to the
  brief's `source`/`rule_text`; `evaluate_concept` already returns `source` and `rule_text` because the API does.
- M0 creates only the directories it uses (`app/`, `tests/`); the other directories in §4 are created by the milestone
  that fills them, rather than as empty placeholders.

## Open questions
- ~~`httpx` vs `httpx2`~~: resolved in M5 (D24): `httpx2`, `httpx` removed; the Starlette `httpx` warning is gone.
  One warning remains, raised inside Starlette itself (`anyio.abc.BlockingPortal` alias); not ours to fix.
- `currently_eligible_member` ignores `requested_date` ("eligible today" relies on a caller-supplied status
  snapshot). Documented, not changed.
- ~~Dependency without an effective version~~: decided in M3, 422 `not_evaluable` (D18).
- Process note: PROGRESS.md in `m2-done` and `m3-done` lacks D13–D20 because an edit script removed the
  "Deviations" heading that later insertions anchored on; restored in `m4-done`.

## Mutation checks run so far
| Milestone | Mutation | Killed by |
|---|---|---|
| M1 | `effective_from > effective_to` → `>=` | `test_effective_from_equal_to_effective_to_is_valid` |
| M1 | overlap `>=` → `>` | `test_approved_versions_must_not_overlap` |
| M1 | report only the first Pydantic error per concept | survived at first → added `test_every_schema_error_within_one_concept_is_reported`, now killed |
| M2 | `lte` → `operator.lt` | 6 tests, e.g. `test_active_member_boundaries[start0-None-True]` |
| M2 | `gte` → `operator.gt` | 5 tests, e.g. `test_active_member_boundaries[start1-end1-True]` |
| M2 | missing = `facts.get(name) is None` (null conflated with absent) | 8+ tests, e.g. `test_active_member_boundaries[start2-None-True]` |
| M2 | `required_facts` ignores `{concept:}` | `test_dependency_facts_are_required_even_when_short_circuited` and others |
| M2 | comparison with null returns `True` | `test_comparison_with_null_is_false[*]` |
| M2 | `not_before` check disabled | `test_coverage_end_before_start_is_invalid`, `test_reporting_month_end_before_start_is_invalid` |
| M4 audit | resolver picks the first of several candidates | 13 tests, e.g. `test_resolve_ambiguous_response_shape` |
| M4 audit | context mismatch falls back silently | 4 tests, e.g. `test_decision_table[solo-...-crm-...]` |
| M4 audit | context narrowing ignores domain | 5 tests, e.g. `test_decision_table[widget-...-ops-...]` |
| M4 audit | `effective_to` treated as exclusive | 8 tests, e.g. `test_get_selects_version_by_as_of[2025-12-31-1.0.0]` |
| M4 audit | drafts become effective | 17 tests |
| M4 audit | resolver ignores visibility | 25 tests |
| M4 audit | `is_visible` always true | 49 tests |
| M4 audit | 403 audited as `ok` | 3 tests, e.g. `test_denied_line` |
| M4 audit | unauthenticated requests not audited | 4 tests, e.g. `test_unauthenticated_line` |
| M4 audit | key comparison accepts any key | 59 tests |
| M4 audit | `X-Via` copied into audit | 4 tests, e.g. `test_via_is_mapped_never_copied[MCP-api]` |
| M4 audit | evaluate skips the 404/403 check | 6 tests |
| M4 audit | first of several `X-API-Key` headers used | `test_several_key_headers_are_401[*]` |
| M4 audit | key comparison on the first digest byte only | survived at first → added `test_no_wrong_key_authenticates`, now killed |

## Developer must be able to explain
- Why validation is split between `models.py` (shape) and `catalog.py` (cross-object), and how one pass collects all
  issues (`app/catalog.py` `parse_catalog`).
- Why `raw_ids` / `raw_fact_names` exist: they stop one schema error cascading into false "unknown target" errors.
- How the callable discriminator picks an expression node type (`app/models.py` `_expr_tag`).
- Why required facts are derived by walking the tree before evaluating, and why `name not in facts` (not
  `facts.get(name) is None`) is the missing test (`app/rules.py` `required_facts`, `evaluate`).
- Why auth sits in middleware before routing (same 401 for unknown path, bad body, wrong method) and how the audit
  line is assembled from `request.state` without reading the body (`app/main.py` `authenticate_and_audit`).
- Why key comparison hashes first and loops over every key (`app/auth.py` `KeyStore.authenticate`).
- Why evaluate checks access before facts (`app/main.py` `evaluate_concept`, `visible_concept`).
- The resolver decision table and why `Resolution` cannot carry a restricted concept (`app/resolver.py` `resolve`).
- How the MCP server picks the key to forward (header on HTTP, env on stdio, never a fallback) and why errors are
  returned as `CallToolResult(is_error=True, structured_content=...)` instead of `ToolError` (`mcp_server/server.py` `_api_key`, `_error`).
- Where comparison semantics live (`app/rules.py` `_COMPARE`, the only op table) and why null compares false.
