# Build Brief: Semantic Architecture API + MCP Service (Ryan-MCP)

**Greenfield build.** The repository contains only `CLAUDE.md` and this file. Everything else is created by you (Claude Code).

How this works: you will receive **three build prompts**. Each covers several milestones (Section 11). Work through the milestones in order, commit and tag each one (`m0-done` …), and stop only at a marked **DESIGN PAUSE** or at the end of a prompt. This brief is the source of truth. The prompts only add process.

---

## 1. Goal

Build a small, credible proof of concept for the client's brief:

> A semantic architecture gives data a shared, machine-readable business meaning. When a person or AI agent asks "What is an active member?", every system uses the same approved definition, calculation, data source, relationships and access rules.

Deliverable: a **read-only glossary API** (FastAPI) plus a **thin MCP server** (Python MCP SDK) that lets an AI agent ask for approved business meanings. The demo must show the client's core problem being solved: the word **"member"** means different things in CRM, enrollment, claims, analytics and customer service, and the service returns the approved meaning for the caller's context, or the candidates plus a clarifying question when the context is ambiguous. It never guesses.

The audience is a client reviewing competence. The developer must be able to explain every design choice, so keep the code small and readable, and comment the *why*.

## 2. Ground rules and scope decisions (already made)

| Decision | Choice |
|---|---|
| Form of "semantic" | Governed business glossary. **Not** an ontology (no RDF/OWL/SKOS/graph DB). |
| Existing catalog tools | None. No integration. |
| Data | Healthcare-flavoured, **synthetic only**. No real data, ever. |
| What the service returns | **Metadata only**: definitions, sources, rules, relationships, governance fields. It does **not** return member rows. |
| Resolution logic | **Deterministic code. No LLM anywhere in the service.** Never pick a definition when more than one fits. |
| Governance | Every concept has owner (steward), version, status (`draft`/`approved`/`deprecated`), effective dates. |
| Identity | API keys mapped to roles, as a **stand-in** for real identity. Say so plainly in the README. |
| Success test | A written set of ambiguous questions the service answers correctly (Section 9). |
| Write access | Read-only. No create/edit endpoints. Concepts live in a YAML file. |

Out of scope: real data sources or warehouses, executing queries, a UI, a database, an OAuth implementation (document the gap only), a governance/approval workflow, RDF/OWL.

## 3. Starting point and lessons carried over

- The repo is empty apart from `CLAUDE.md` and this brief. There is no legacy code to preserve or port.
- Target **Python 3.11**, FastAPI, Pydantic v2, pytest, httpx. Pin exact current stable versions, verified to install on 3.11. Keep runtime (`requirements.txt`) and test tooling (`requirements-dev.txt`) separate.
- An earlier throwaway prototype made these mistakes. **Do not repeat them:**
  1. Errors returned as HTTP 200 with a `detail` message.
  2. A 500 on bad input (dates typed as plain strings, malformed nested context).
  3. A `false` result returned when needed context was missing, which looks like a real answer.
  4. Each rule implemented three times (text, query branch, resolve branch), which contradicts "define once".
  5. An unauthenticated endpoint that dumped full member records.
  6. Invalid seed data: coverage end dates before start dates, and a `status` field contradicting the coverage-date rule.
  7. Virtualenvs and caches shipped in the archive, hard-coded absolute paths, test and runtime packages mixed together.
- **Carry over these three definitions verbatim** (ids, wording, rules). Nothing else from that prototype survives.

  - `active_member`
    - definition: "Person with coverage effective on the requested date"
    - authoritative source: `eligibility_enrollment_platform`
    - rule text: `coverage_start_date <= requested_date AND (coverage_end_date IS NULL OR coverage_end_date >= requested_date)`
    - source systems: `eligibility_enrollment_platform`, `crm`, `claims_system`
  - `member_in_network`
    - definition: "Member whose provider network status is in-network on the requested date"
    - authoritative source: `provider_network_directory`
    - rule text: `network_status == 'in_network' AND active_member == TRUE`
    - source systems: `provider_network_directory`, `eligibility_enrollment_platform`, `care_management_system`
  - `eligible_for_follow_up`
    - definition: "Active member eligible for outreach based on plan status and care gap follow-up rules"
    - authoritative source: `care_management_system`
    - rule text: `active_member == TRUE AND follow_up_eligible == TRUE AND risk_flag != 'high'`
    - source systems: `care_management_system`, `eligibility_enrollment_platform`, `population_health_platform`

## 4. Target architecture

```
AI agent (Claude Desktop / Inspector / custom)
        │  MCP (streamable HTTP; stdio for local testing)
        ▼
mcp_server/   thin wrapper, 3 tools, forwards the caller's API key
        │  HTTP
        ▼
app/          FastAPI: auth → resolver / catalog / rules → audit
        │
        ▼
catalog/concepts.yaml   (validated at startup; app refuses to start if invalid)
```

Layout (adjust only with a stated reason):

```
app/        main.py  models.py  catalog.py  resolver.py  rules.py  auth.py  audit.py  errors.py
catalog/    concepts.yaml
config/     api_keys.example.yaml
mcp_server/ server.py
evals/      ambiguous_questions.yaml  run.py
tests/      test_catalog.py test_rules.py test_resolver.py test_api.py test_auth.py test_audit.py test_mcp.py  fixtures/
scripts/    demo_conflict.py
docs/       ASSUMPTIONS.md  DEMO_SCRIPT.md
README.md  PROGRESS.md  requirements.txt  requirements-dev.txt  .gitignore  .gitattributes  pytest.ini
```

## 4a. Data model (Pydantic, validated at load)

**Concept** fields:

- `id` (snake_case), `term` (the business word, for example `member`), `name` (human label)
- `context`: `{system: crm|enrollment|claims|analytics|customer_service|care_management|network, domain: str}`
- `definition` (plain English), `aliases` (list of strings)
- `authoritative_source`: `{system, dataset}`; `source_systems` (other systems holding the data)
- `rule`: `{text, expression}`. `text` is human-readable, and `expression` is the declarative rule the evaluator executes (Section 5).
- `relationships`: list of `{type: depends_on | alternative_meaning_of | supersedes, target: <concept id or term>, description}`
- Governance: `owner` (a team, not a named person), `version` (semver), `status`, `effective_from`, `effective_to` (nullable), `superseded_by` (nullable)
- Access: `allowed_roles` (list)

**Startup validation** (fail fast, with clear errors, collecting all errors in one pass, each naming the concept id and field): unique `(id, version)` pairs (a concept id appears once per version, which is how effective-dated versions are stored); relationship and `superseded_by` targets exist; `effective_from <= effective_to`; every `approved` concept has `owner`, `rule` and `authoritative_source`; every `deprecated` concept has `superseded_by`; no two `approved` versions of the same concept id overlap in effective dates; no dependency cycles; every rule expression is structurally valid.

## 4b. Seed catalog (all synthetic, in `catalog/concepts.yaml`)

The term **`member`** must have five approved, context-scoped concepts (from the client's brief):

| id | context.system | Meaning | Facts the rule needs |
|---|---|---|---|
| `registered_member` | crm | Person who registered or expressed interest | `registration_date` |
| `active_member` | enrollment | Coverage effective on the requested date (Section 3, verbatim) | `coverage_start_date`, `coverage_end_date` |
| `claimant_member` | claims | Person with at least one claim to date | `claim_count_to_date` |
| `reporting_month_member` | analytics | Person enrolled for any portion of a reporting month | `coverage_start_date`, `coverage_end_date`, `reporting_month_start`, `reporting_month_end` |
| `currently_eligible_member` | customer_service | Person eligible today | `eligibility_status` |

Also seed `member_in_network` (depends_on `active_member`) and `eligible_for_follow_up` (depends_on `active_member`; **restricted**: `allowed_roles: [care_manager, steward]`), both verbatim from Section 3.

Governance demos to include: (a) one concept with **two effective-dated versions** (use `reporting_month_member`: v1 ended, v2 current), so `as_of` resolution is demonstrable; (b) **one deprecated concept** with `superseded_by`; (c) one `draft` concept that must never resolve.

Optional stretch (only after everything else is done): a second term such as `revenue` with two candidates (recognised vs billed).

## 5. Rules: define once, execute from data

Each rule is **one declarative expression** in the YAML, executed by a small evaluator in `app/rules.py`. **No `eval()`/`exec()`.** Suggested shape (adapt if you have a cleaner one):

```yaml
expression:
  all:
    - {fact: coverage_start_date, op: lte, value: {ref: requested_date}}
    - any:
        - {fact: coverage_end_date, op: is_null}
        - {fact: coverage_end_date, op: gte, value: {ref: requested_date}}
```

- Ops: `eq ne lt lte gt gte is_null not_null in`; combinators: `all any not`; `{ref: requested_date}` resolves to the request date; `{concept: active_member}` evaluates another concept (use this for `depends_on`; detect cycles at load).
- **Required facts are derived statically from the expression**, not hand-listed and not discovered during evaluation. Otherwise short-circuiting in `any`/`all`/`not` could return a boolean while a needed fact was missing.
- **Missing vs null.** A fact key that is absent is *missing*. A fact key present with `null` is a valid value (an open-ended coverage end date). Never conflate them.
- Missing facts → **422** with `error.code = "insufficient_context"` and `missing_facts: [...]`. Never return a boolean when facts are missing. Bad date formats → 422. A coverage range with end before start → 422 `invalid_facts` (choose the simplest mechanism; if it needs more than ~20 lines, propose a cheaper alternative).
- Keep `rule.text` authored by hand. Test that every approved concept has both `text` and `expression`. Note in the README that keeping them in sync is a stewardship responsibility (known limitation).

## 6. API contract

All under a `/semantic` prefix except `/health` (public). Every non-health request requires `X-API-Key`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness, public |
| GET | `/semantic/concepts?term=&status=&system=` | List concepts. Restricted ones appear as `{id, name, term, access: "restricted"}` with **no definition**. |
| GET | `/semantic/concepts/{id}?as_of=` | Full definition (version effective on `as_of`, default today). **403** if the role is not allowed, with `required_role`. **404** if unknown. |
| GET | `/semantic/concepts/{id}/relationships` | Outgoing and incoming relationships (incoming derived) |
| POST | `/semantic/resolve` | Term plus context → approved meaning (below) |
| POST | `/semantic/concepts/{id}/evaluate` | Evaluate the rule against **caller-supplied** facts: `{requested_date, facts: {...}}` → `{result, rule_text, source, version, status}`. The access check applies to the **target concept only**; concepts it depends on are evaluated internally and their definitions are not exposed. The version used is the one effective on `requested_date`. Draft concepts return 404; deprecated concepts evaluate with a warning. |
| GET | `/semantic/audit?limit=` | Recent audit entries; `steward` only (optional; build only if trivial) |

**Error format** (all errors): `{"error": {"code": "...", "message": "...", "details": {...}}}`, with correct status codes (401, 403, 404, 422). **No error returns HTTP 200.** Type `requested_date`/`as_of` as real dates so Pydantic rejects bad input with 422 instead of a 500. A catch-all handler returns a 500 envelope without a stack trace, but no input should ever reach it.

**`POST /semantic/resolve`** request: `{term, context?: {system?, domain?}, as_of?}`. Response (always HTTP 200 for these four outcomes):

```json
{
  "status": "resolved | ambiguous | not_found | restricted",
  "term": "member",
  "as_of": "2026-09-19",
  "concept": { "...full definition incl. source, rule_text, version, status, effective dates, owner..." },
  "candidates": [{"id": "active_member", "context": {"system": "enrollment"}, "definition": "…"}],
  "clarifying_question": "Which meaning of 'member' do you need: crm, enrollment, claims, analytics or customer_service?",
  "suggestions": ["member"],
  "restricted_count": 0,
  "warnings": []
}
```

Resolver algorithm (deterministic; a **pure function** taking catalog, term, context, `as_of` and caller, so it is testable without HTTP and reusable by the eval runner):

1. Normalise the term: lowercase, trim, collapse whitespace, treat `_` and `-` as spaces.
2. Match against concept `id`, `name`, `aliases` and `term`.
3. Keep only concepts effective on `as_of`. Exclude `draft`. `deprecated` concepts resolve **with a warning** and `superseded_by`.
4. If `context.system` or `context.domain` is given, narrow candidates to those contexts. **If the context matches none of the candidates, do not silently fall back**: return `ambiguous` with all visible candidates and a warning saying the context matched nothing.
5. Exactly one **visible** candidate → `resolved`. More than one visible → `ambiguous` (list the visible ones, plus a clarifying question). Zero matches → `not_found` with `suggestions` using simple deterministic similarity (`difflib`). Matches exist but none are visible to the caller's role → `restricted`. If restricted candidates also matched alongside visible ones, include `restricted_count` (a number only, never their details) and add a warning, so a caller is never told "resolved" without knowing another meaning exists that they cannot use.
6. **Never choose between multiple candidates.** Candidate ordering is stable and documented.

## 7. Access control and audit

- **Roles**: `analyst`, `care_manager`, `steward`. Keys come from an env var or a YAML file **outside version control**; commit only `config/api_keys.example.yaml` with obviously fake keys. Use `hmac.compare_digest`. Missing or invalid key → 401. Misconfiguration (no keys, malformed file) fails at startup with a clear message.
- **Restricted concepts**: existence is visible (list and resolve say "restricted"), content is withheld (403 on direct GET). Make this policy a single, clearly named setting so it is easy to flip.
- **Audit log**: append-only JSONL, path configurable, safe under concurrent requests in one process. One line per request and per MCP tool call: timestamp, key label (never the key), role, endpoint or tool, parameters, outcome (`ok`, `denied`, `not_found`, `ambiguous`, `error`, `unauthenticated`), and `via` (`api` or `mcp`, from a header the MCP server sets; treat it as untrusted metadata, never for authorization). **Denied and unauthenticated attempts are logged too.**
- A key value must never appear in logs, audit lines, error bodies, the OpenAPI document or tracebacks.
- Decide and document whether `/docs` and `/openapi.json` require a key (default: public for the demo, documented).
- README must state: API keys are a demo stand-in; production would use OAuth per the current MCP authorization spec (**check the current spec** and summarise the gap in `docs/ASSUMPTIONS.md`).

## 8. MCP server

- Use the official Python MCP SDK. **Do not rely on memory of its API, because it changes quickly: read the current docs (or the installed package's source), pin the exact version in `requirements.txt`**, and record in `PROGRESS.md` the version, the transport API used, and how request headers are read inside a tool on HTTP transport.
- Transports: **streamable HTTP** as the primary (the service is meant to be remote and org-wide) and **stdio** for local Inspector testing.
- **Three tools maximum**, with carefully written descriptions, because the agent reads them to decide what to call:
  1. `resolve_term(term, context?, as_of?)`: wraps `/semantic/resolve`. The description must tell the agent to call this **before** answering any question that uses a business term, and to ask the user the returned clarifying question rather than guess when `status` is `ambiguous`.
  2. `get_definition(concept_id, as_of?)`: wraps `GET /concepts/{id}` and includes relationships.
  3. `evaluate_concept(concept_id, requested_date, facts)`: wraps `/evaluate`.
- Tool results are **structured** (include `status`, `source`, `rule_text`, `version`, `effective_from/to`, `warnings`), not prose. Errors are returned as structured tool errors, not stack traces.
- The MCP server is a **thin wrapper**: no business logic, no catalog access. It forwards the caller's API key (from a request header on HTTP transport, or an env var on stdio) so roles and audit apply end to end, and sends `X-Via: mcp`.

## 9. Tests and the ambiguous-question eval

**Unit and API tests** (pytest). At minimum:

- Rules: `active_member` boundaries (start == requested, end == requested, end null, start after requested, end before requested); every op and combinator; missing vs null including the short-circuit case; cycle detection; malformed expressions.
- Resolver: exact match, alias, case/whitespace, ambiguous (no context), narrowed by context, context matches nothing, not_found plus suggestion (`membr` → `member`), `as_of` version selection, deprecated warning, draft never resolves, restricted, mixed visible/restricted. Enumerate the full decision table (match count × visibility × context × status × `as_of`), so every cell has defined behaviour and a test.
- API: every endpoint, every error code; a route-wide test that every error case returns non-200 with the envelope; a table of hostile inputs (empty, whitespace-only or very long term, unicode, null or wrong-typed context, bad dates, missing body) that never produces a 5xx.
- Auth: 401/403 matrix by role × endpoint; restricted list entries expose no definition; startup fails clearly on bad key configuration.
- Audit: a line is written for success **and** denial and unauthenticated attempts; never contains the key; newlines in a header value cannot forge a second line. A **key-leak test** sends a distinctive key and scans logs, audit file, error bodies and the OpenAPI document for it.
- Catalog: each startup validation rule fails with a clear message on a bad fixture.
- MCP: tools are listed with descriptions; a tool call returns structured output; API key forwarding works; also verified against the real protocol (stdio subprocess and HTTP), not only unit tests.

Assertions must be **exact** (specific ids, statuses, counts, codes), not `count >= 1`. **Mutation check:** for at least six critical behaviours (resolver ambiguity, context narrowing, effective dates, restricted visibility, missing-vs-null, audit on denial, key comparison), break the code, confirm a test fails, restore. Report as a table. Any surviving mutant gets a new test.

**Eval** in `evals/ambiguous_questions.yaml` (10 to 15 cases; each has question text, role, context, `as_of`, expected status, expected concept ids, rationale), runnable with `python -m evals.run` (prints a pass/fail table, exits non-zero on failure). At least these cases:

1. "member", no context → `ambiguous`, exactly 5 candidates.
2. "member", system=enrollment → `resolved: active_member`.
3. "member", system=crm → `resolved: registered_member`.
4. "member", system=customer_service → `resolved: currently_eligible_member`.
5. "Active  Member" (case/whitespace) → `resolved: active_member`.
6. "member", system=finance (matches nothing) → `ambiguous` with a context-matched-nothing warning.
7. "eligible for follow up" as `analyst` → `restricted`; as `care_manager` → `resolved`.
8. "membr" → `not_found` with suggestion `member`.
9. "reporting month member" with `as_of` before/after the version change → different `version`.
10. The draft concept → `not_found`.

Also run the three core cases end to end through the MCP tool path.

**Demo script** `scripts/demo_conflict.py`: using a synthetic facts fixture (in `tests/fixtures/`, **not** in the API), evaluate each of the five `member` definitions over the same population of 50 synthetic people on one date and print the counts side by side, then print 2–3 individuals who are a "member" under one definition and not another, with a one-line reason. The fixture generator is deterministic (seeded), coverage end is never before start, and each person carries every fact every definition needs. **Never expose it through the API.**

## 10. Hygiene and docs

- `git init`; `.gitignore` (venvs, `__pycache__`, `.pytest_cache`, `*.pyc`, `.env`, real key files, audit logs); `.gitattributes` with `text=auto`. If a virtualenv is needed, use `.venv` (git-ignored). Prove reproducibility once by installing `requirements-dev.txt` into a throwaway venv **outside** the repo and running the tests.
- `README.md`: what it is, architecture, run instructions (Windows and Linux/macOS, no hard-coded paths), auth setup, the three tools, how to run tests and evals, **limitations** (API keys stand-in, `rule.text`/`expression` sync, in-memory catalog, metadata-only).
- `docs/DEMO_SCRIPT.md`: a 5-minute walkthrough with exact commands (PowerShell and bash where they differ) and expected outputs pasted from real runs: same word → different numbers (`demo_conflict`) → agent asks about an ambiguous term → clarifying question → narrowing by context → restricted concept as analyst → audit trail → run the eval. One line on what to do if each of the top three steps fails.
- `docs/ASSUMPTIONS.md`: the decisions in Section 2, the restricted-visibility policy, the error semantics, honest limitations, and **what I would do differently in production** (OAuth per the current MCP authorization spec, catalog integration, persistence, stewardship workflow, rule/text sync, caching).
- **Docs follow the code**: every command and claim must be executed or verified; mark anything you could not run as unverified. Style: plain and specific, no marketing adjectives ("governed", "enterprise-grade", "secure" are not claims; describe what exists).
- Each module starts with a short comment on **why** it is designed this way.
- Final fresh-clone test: clone into a temp directory, follow the README from scratch in a new venv, and run tests, eval and demo commands literally.

## 11. Milestones and prompts

| # | Milestone | Prompt | Done when |
|---|---|---|---|
| M0 | Scaffold | 1 | Git initialised; layout, requirements, `.gitignore`/`.gitattributes`, `/health` and its test; tests green in a fresh throwaway venv |
| M1 | Models, catalog, validation | 1 | YAML loads; every startup validation has a passing and a failing test with an exact error assertion |
| M2 | Rule evaluator (pure library, no HTTP) | 1 | Boundary, missing-vs-null (incl. short-circuit), cycle and malformed-expression tests pass; no `eval()`; each rule exists once (in the YAML) |
| M3 | API foundation: error envelope, auth, roles, audit, concept endpoints, `/evaluate` | 2 | 401/403 matrix, key-leak test and audit tests pass; no error returns 200 |
| M4 | Resolver + `POST /resolve` + robustness | 2 | Decision-table tests pass; hostile inputs never produce a 5xx; route-wide error test passes |
| M5 | MCP server | 3 | Runs over stdio and HTTP; 3 tools; SDK version pinned; verified through a real protocol client |
| M6 | Test hardening, eval, demo script | 3 | Eval passes; mutation table complete; `demo_conflict.py` prints differing counts |
| M7 | Docs | 3 | README, DEMO_SCRIPT, ASSUMPTIONS accurate; fresh-clone test passes |

**DESIGN PAUSES** (post the design and wait for the developer's "go"; no code before it):

- **DP1**, start of Prompt 1: the data model, the rule-expression schema, how concepts and versions are keyed and referenced, the seed catalog as a table, the evaluator's approach to missing-vs-null, and any problems you see in this brief.
- **DP2**, start of Prompt 2: auth and key design, the error contract, the audit fields, the restricted-visibility setting, and the resolver decision table.

## 12. Working rules for you (Claude Code)

- Follow `CLAUDE.md`. Keep `PROGRESS.md` as a running log: status, decisions with the alternative you rejected, deviations, open questions, and a "developer must be able to explain" list.
- Do not add features, endpoints, tools or dependencies beyond this brief. If something is ambiguous, ask instead of assuming, and record any assumption in `PROGRESS.md` (they feed `docs/ASSUMPTIONS.md`).
- No LLM calls, no `eval()`/`exec()`, no real data, no committed secrets.
- Before reporting the end of a prompt, audit your own work as an independent reviewer would: re-run everything, verify each acceptance criterion with evidence you produce, and hunt for scope creep and hard-rule violations. Paste real output.
- If you deviate from this brief, say so explicitly and why.

## 13. Definition of done

- [ ] All tests pass; the eval passes; `demo_conflict.py` shows the five definitions giving different counts.
- [ ] `resolve_term("member")` with no context returns `ambiguous` with 5 candidates and a clarifying question, through both the API and the MCP tool.
- [ ] Each rule is defined **once** (in YAML) and executed from there.
- [ ] No error path returns HTTP 200, and no input can cause a 500.
- [ ] A missing fact never yields a boolean result.
- [ ] Restricted concepts hide their definition from unauthorised roles, and every request (including denials and unauthenticated attempts) is audited.
- [ ] MCP server has at most 3 tools with structured output, and its SDK version is pinned.
- [ ] Repo contains no virtualenvs, caches, secrets or hard-coded paths, and the docs match the code.

## 14. Human steps (not for Claude Code)

The developer will: approve DP1 and DP2; read the resolver, rules and auth code until they can explain them; test the MCP server in MCP Inspector and Claude Desktop with the eval questions; review `docs/ASSUMPTIONS.md`; and rehearse the demo script.
