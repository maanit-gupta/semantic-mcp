# PROGRESS

Running log for the Ryan-MCP build. The spec is `BUILD_BRIEF.md`; this file records status, decisions
(with the alternative rejected), deviations and open questions.

## Status

| Milestone | Status | Tag |
|---|---|---|
| M0 Scaffold | done | `m0-done` |
| M1 Models, catalog, validation | not started | |
| M2 Rule evaluator | not started | |
| M3–M7 | not started (later prompts) | |

## DP1 (approved 2026-09-19, developer's "go" with all four default answers)

1. Python 3.11 installed with Homebrew on the dev machine (only 3.12/3.14 were present).
2. A top-level fact dictionary in `concepts.yaml`, and a check that `depends_on` equals expression `{concept:}` refs.
3. A comparison against `null` is `false` (SQL `WHERE` semantics); `eq`/`ne` with a `null` literal is rejected at load.
4. Deprecated = `covered_life`; draft = `engaged_member` (term `member`); `reporting_month_member` v1 "first day of month" → v2 "any portion".

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

## Deviations from the brief
- D1 fact dictionary and D2 `{fact:}` operand (both approved at DP1).
- D6 extra validation rule (approved at DP1).
- M0 creates only the directories it uses (`app/`, `tests/`); the other directories in §4 are created by the milestone
  that fills them, rather than as empty placeholders.

## Open questions
- Starlette 1.6 warns that `httpx` is deprecated for `TestClient` and suggests `httpx2`. Kept `httpx` because the
  brief names it and the MCP SDK (M5) depends on it; revisit if Starlette drops `httpx` support.
- `currently_eligible_member` ignores `requested_date` ("eligible today" relies on a caller-supplied status
  snapshot). Documented, not changed.
- If a `{concept:}` dependency has no effective version on `requested_date`, the evaluator raises
  `ConceptNotEffective`; M3 decides its status code.

## Developer must be able to explain
(filled in as milestones land)
