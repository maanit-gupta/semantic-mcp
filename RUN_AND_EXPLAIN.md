# Semantic Glossary API + MCP Service — Run & Explain Guide

A single reference for running this project locally on macOS or Windows, and for understanding what every result means.

---

## 1. What this project is, in one paragraph

In a large organisation the same business word means different things in different systems. "Member" in CRM means someone who registered. In enrollment it means someone with active coverage. In claims it means someone who has filed a claim. When a person — or an AI agent — asks "how many members do we have?", the answer depends entirely on which definition is used, and nobody says which one. This project is a **governed business glossary served over an API**, plus a **thin MCP server** so an AI agent can consult it. Ask it about a term and it returns the approved definition, where the data lives, how it is calculated, who owns it, which version applies on a given date, and who is allowed to see it. When more than one approved meaning fits, it refuses to choose and asks a clarifying question instead.

**Scope, stated plainly:** it serves *meaning*, not data. It holds no records about people. Synthetic, healthcare-flavoured content throughout. API keys stand in for real identity. No LLM is involved in deciding anything — resolution is deterministic code.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11 | Other 3.x versions likely work, but 3.11 is what everything was verified on |
| Git | To clone the repository |
| Node.js | Only for MCP Inspector (Section 6). Not needed for anything else |
| Claude Desktop | Only for the agent demo (Section 7) |

The project is cross-platform by construction: `pathlib` for paths, `python -m` for everything, no shell scripts. It was verified on macOS. Windows is expected to work and is untested.

---

## 3. MASTER COMMANDS — macOS / Linux

Run from the repository root. Replace `~/semantic_mcp` with wherever you cloned it.

### 3.1 One-time setup

```bash
cd ~/semantic_mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp -n config/api_keys.example.yaml config/api_keys.yaml
```

### 3.2 The three proof commands

```bash
python -m pytest -q                 # the full test suite
python -m evals.run                 # the ambiguous-question evaluation
python -m scripts.demo_conflict     # the "same word, different numbers" demo
```

### 3.3 Start the API

Leave this running in its own terminal.

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --port 8000
```

### 3.4 Query the API (second terminal)

```bash
cd ~/semantic_mcp
source .venv/bin/activate

ANALYST="FAKE-analyst-key-for-local-demo-only"
CARE="FAKE-care-manager-key-for-local-demo-only"
STEWARD="FAKE-steward-key-for-local-demo-only"

call() {   # usage: call METHOD PATH [KEY] [JSON_BODY]
  local m=$1 u=$2 k=$3 b=$4 tmp; tmp=$(mktemp)
  local a=(-s -o "$tmp" -w 'HTTP %{http_code}\n' -X "$m" "http://127.0.0.1:8000$u")
  [[ -n $k ]] && a+=(-H "X-API-Key: $k")
  [[ -n $b ]] && a+=(-H 'Content-Type: application/json' -d "$b")
  curl "${a[@]}"; python3 -m json.tool "$tmp" 2>/dev/null || cat "$tmp"; rm -f "$tmp"
}
```

Then:

```bash
# 1  ambiguous term — refuses to guess
call POST /semantic/resolve "$ANALYST" '{"term":"member"}'

# 2  with context — one approved meaning
call POST /semantic/resolve "$ANALYST" '{"term":"member","context":{"system":"enrollment"}}'

# 3  contradicting context — no silent fallback
call POST /semantic/resolve "$ANALYST" '{"term":"active member","context":{"system":"crm"}}'

# 4  typo — deterministic suggestion, no invention
call POST /semantic/resolve "$ANALYST" '{"term":"membr"}'

# 5  access control — count only, then 403, then allowed
call POST /semantic/resolve "$ANALYST" '{"term":"eligible for follow up"}'
call GET  /semantic/concepts/eligible_for_follow_up "$ANALYST"
call POST /semantic/resolve "$CARE"    '{"term":"eligible for follow up"}'

# 6  versioned definitions
call GET "/semantic/concepts/reporting_month_member?as_of=2025-06-01" "$ANALYST"
call GET "/semantic/concepts/reporting_month_member?as_of=2026-09-20" "$ANALYST"

# 7  rule execution, and refusal when facts are missing
call POST /semantic/concepts/active_member/evaluate "$ANALYST" \
  '{"requested_date":"2026-09-20","facts":{"coverage_start_date":"2026-01-01","coverage_end_date":null}}'
call POST /semantic/concepts/member_in_network/evaluate "$ANALYST" \
  '{"requested_date":"2026-09-20","facts":{"network_status":"out_of_network"}}'

# 8  no key, then the audit trail
call GET /semantic/concepts/active_member
call GET "/semantic/audit?limit=10" "$STEWARD"
```

Quote any path containing `?` — zsh treats it as a wildcard.

### 3.5 Run the MCP server

```bash
export PYTHONPATH="$PWD"
export SEMANTIC_API_URL="http://127.0.0.1:8000"
export SEMANTIC_API_KEY="$ANALYST"

python -m mcp_server.server                              # stdio transport
python -m mcp_server.server --transport http --port 8001 # HTTP transport
```

---

## 4. MASTER COMMANDS — Windows (PowerShell)

Unverified: the code is cross-platform, but these exact commands were not run on Windows. Replace `C:\semantic_mcp` with your clone path.

### 4.1 One-time setup

```powershell
cd C:\semantic_mcp
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item config\api_keys.example.yaml config\api_keys.yaml
```

If activation is blocked: `Set-ExecutionPolicy -Scope Process Bypass`

### 4.2 The three proof commands

```powershell
python -m pytest -q
python -m evals.run
python -m scripts.demo_conflict
```

### 4.3 Start the API

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --port 8000
```

### 4.4 Query the API (second terminal)

```powershell
cd C:\semantic_mcp
.\.venv\Scripts\Activate.ps1

$ANALYST = "FAKE-analyst-key-for-local-demo-only"
$CARE    = "FAKE-care-manager-key-for-local-demo-only"
$STEWARD = "FAKE-steward-key-for-local-demo-only"

function call($method, $route, $key, $body) {
  $p = @{ Method = $method; Uri = "http://127.0.0.1:8000$route"; UseBasicParsing = $true; Headers = @{} }
  if ($key)  { $p.Headers["X-API-Key"] = $key }
  if ($body) { $p.Body = $body; $p.ContentType = "application/json" }
  try   { $r = Invoke-WebRequest @p; "HTTP $([int]$r.StatusCode)"; $r.Content | ConvertFrom-Json | ConvertTo-Json -Depth 10 }
  catch {
    "HTTP $([int]$_.Exception.Response.StatusCode)"
    if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message | ConvertFrom-Json | ConvertTo-Json -Depth 10 }
  }
}
```

Then the same eight demo calls, in PowerShell syntax (note the single quotes around JSON):

```powershell
call POST /semantic/resolve $ANALYST '{"term":"member"}'
call POST /semantic/resolve $ANALYST '{"term":"member","context":{"system":"enrollment"}}'
call POST /semantic/resolve $ANALYST '{"term":"active member","context":{"system":"crm"}}'
call POST /semantic/resolve $ANALYST '{"term":"membr"}'
call POST /semantic/resolve $ANALYST '{"term":"eligible for follow up"}'
call GET  /semantic/concepts/eligible_for_follow_up $ANALYST
call POST /semantic/resolve $CARE '{"term":"eligible for follow up"}'
call GET  "/semantic/concepts/reporting_month_member?as_of=2025-06-01" $ANALYST
call GET  "/semantic/concepts/reporting_month_member?as_of=2026-09-20" $ANALYST
call POST /semantic/concepts/active_member/evaluate $ANALYST '{"requested_date":"2026-09-20","facts":{"coverage_start_date":"2026-01-01","coverage_end_date":null}}'
call POST /semantic/concepts/member_in_network/evaluate $ANALYST '{"requested_date":"2026-09-20","facts":{"network_status":"out_of_network"}}'
call GET  /semantic/concepts/active_member
call GET  "/semantic/audit?limit=10" $STEWARD
```

### 4.5 Run the MCP server

```powershell
$env:PYTHONPATH = "C:\semantic_mcp"
$env:SEMANTIC_API_URL = "http://127.0.0.1:8000"
$env:SEMANTIC_API_KEY = $ANALYST

python -m mcp_server.server
python -m mcp_server.server --transport http --port 8001
```

---

## 5. Reading the results

### 5.1 `python -m pytest -q`

```
651 passed, 1 warning in 18.17s
```

**What it justifies.** The behaviour is pinned down, not merely present. Beyond ordinary coverage, three specific classes of test matter to a reviewer:

- **Mutation checks.** During the build, the code was deliberately broken in over 30 places (a `<=` changed to `<`, the visibility check forced to `true`, the key comparison shortened to one byte) and the suite was confirmed to catch each one. One mutation survived and a new test was written to kill it. This is evidence the tests would *notice* a regression, which a passing count alone never proves.
- **Hostile-input tests.** Around 50 malformed requests — empty terms, unicode, wrong types, invalid dates, missing bodies — confirm that no input produces a 5xx and no error ever returns HTTP 200.
- **Security tests.** A key-leak scan sends a distinctive API key and then searches response bodies, response headers, the audit file, log records, stdout/stderr and the OpenAPI document for any trace of it. Another test tries 5,000 wrong keys plus every one-character near-miss of each real key.

The single warning originates inside Starlette, not this codebase.

### 5.2 `python -m evals.run`

```
16/16 checks passed (13 cases; 3 also via MCP)
```

**What it is.** A behavioural evaluation, separate from unit tests: thirteen realistic ambiguous questions, each with a declared expected outcome. Three of them are run twice — once against the resolver directly, once through the MCP tool path — which proves an agent gets the same answer a direct API caller does.

**What it justifies.** Unit tests show the parts work. The eval shows the *system* answers the questions the client actually cares about: "member" with no context, "member" narrowed by each system, a term whose context matches nothing, a restricted term seen by two different roles, a typo, a date-dependent version change, and a draft definition that must never surface.

### 5.3 `python -m scripts.demo_conflict`

```
registered_member=47  active_member=24  claimant_member=22
reporting_month_member=32  currently_eligible_member=23
```

**What it is.** One population of 50 synthetic people, one date, five approved definitions of "member", evaluated by the same rules engine the API uses.

**What it means.** This is the whole business case in one line. Five teams asking "how many members do we have?" get 47, 24, 22, 32 and 23 — all correct, all different, because they are answering different questions without realising it. A number in a report is meaningless without the definition attached. The script also prints individuals who qualify under one definition and not another, with the reason, so the difference is concrete rather than abstract.

This is the strongest opening for a client demo.

### 5.4 The eight API calls

| # | Call | Response | What it demonstrates |
|---|---|---|---|
| 1 | `resolve` "member" | `200 ambiguous`, 5 candidates, clarifying question | **The core behaviour.** Five approved meanings fit, so it returns all five and asks which is needed. It does not pick the most common, the first alphabetically, or the one with the best match score. Refusing to guess *is* the feature. |
| 2 | `resolve` + `system: enrollment` | `200 resolved: active_member` | With context, exactly one meaning applies. The response carries the definition, `authoritative_source` (system *and* dataset of record), the rule as text and as executable expression, `owner`, `version`, `status` and effective dates. Everything needed to defend a number in a meeting. |
| 3 | `resolve` "active member" + `system: crm` | `200 ambiguous` + warning | There is no CRM meaning of "active member". A naive system would ignore the context and answer anyway. This one reports that the context matched nothing and shows what *is* available. Silent fallback is how wrong numbers reach dashboards. |
| 4 | `resolve` "membr" | `200 not_found`, suggests "member" | The suggestion comes from deterministic string similarity, not a language model. Nothing is invented, and suggestions are drawn only from concepts this caller may see. |
| 5 | Restricted concept, three calls | `restricted` (count only) → `403` → `resolved` | Access control is part of the meaning. The analyst learns that one meaning exists but sees no id, name, alias or definition text — only a count. The direct fetch returns 403 naming the required roles. The care manager gets the full definition. |
| 6 | Same concept, two `as_of` dates | `1.0.0` then `2.0.0` | Definitions change. Asking "what did this mean last June?" returns last June's version, which is what reproducing a historical report requires. |
| 7 | `evaluate` twice | `200 result: true`, then `422 insufficient_context` | The rule executes from the same YAML the definition is published from, so the documented rule and the executed rule cannot drift. The second call is the important one: a fact is missing, and rather than short-circuiting to `false` it names exactly which facts are needed. A confident `false` here would be a silent wrong answer. |
| 8 | No key, then audit | `401`, then the log | Every request is authenticated before routing, so an unauthenticated caller learns nothing about which paths exist. Every request is logged — successes, denials and unauthenticated attempts — with the key's *label*, never its value. |

### 5.5 Error responses

Every error shares one shape:

```json
{"error": {"code": "...", "message": "...", "details": {...}}}
```

with a correct HTTP status. `401 unauthenticated`, `403 forbidden` (with `required_role`), `404 not_found`, `422 validation_error` / `insufficient_context` / `invalid_facts`.

**Why this matters more than it sounds.** An AI agent cannot reason about failure if failure is indistinguishable from success. The earlier prototype returned HTTP 200 with an error message inside the body, crashed with a 500 on a malformed date, and returned `false` when it lacked the facts to decide. All three produce confidently wrong agent behaviour. Note that `not_found` and `restricted` from `resolve` are `200`, because they are *answers* ("no approved meaning exists", "one exists that you cannot see"), not failures.

---

## 6. MCP Inspector (optional, needs Node.js)

Verifies the MCP layer outside of tests. Keep the API running.

**macOS**

```bash
export PYTHONPATH="$PWD"
export SEMANTIC_API_URL="http://127.0.0.1:8000"
export SEMANTIC_API_KEY="$ANALYST"
npx @modelcontextprotocol/inspector "$PWD/.venv/bin/python" -m mcp_server.server
```

**Windows**

```powershell
$env:PYTHONPATH = "C:\semantic_mcp"
$env:SEMANTIC_API_URL = "http://127.0.0.1:8000"
$env:SEMANTIC_API_KEY = $ANALYST
npx @modelcontextprotocol/inspector C:\semantic_mcp\.venv\Scripts\python.exe -m mcp_server.server
```

If the Inspector does not pass your environment to the child process, enter those three variables in its environment panel.

**What to try**

1. `resolve_term` with `{"term":"member"}` → `ambiguous`, 5 candidates, clarifying question.
2. `get_definition` with `{"concept_id":"eligible_for_follow_up"}` → an **error result** (`forbidden`) containing no definition text.
3. Swap the key to the care-manager key and repeat → the definition appears.

For HTTP transport: run the server with `--transport http --port 8001`, choose "Streamable HTTP" in the Inspector, point it at `http://127.0.0.1:8001/mcp`, and add the header `X-API-Key: <key>`.

---

## 7. Claude Desktop — the agent demo

This is the demo that lands with a client, because they see an AI agent behave correctly rather than a JSON response.

**macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows** — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ryan-mcp-glossary": {
      "command": "/Users/YOU/semantic_mcp/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "PYTHONPATH": "/Users/YOU/semantic_mcp",
        "SEMANTIC_API_URL": "http://127.0.0.1:8000",
        "SEMANTIC_API_KEY": "FAKE-analyst-key-for-local-demo-only"
      }
    }
  }
}
```

On Windows use the same structure with `\\.venv\\Scripts\\python.exe` and escaped backslashes throughout.

Restart Claude Desktop with the API running, then ask:

> **"How many members do we have?"**

**What should happen.** The agent calls `resolve_term`, receives `ambiguous`, and **asks you which meaning you want** instead of answering. Reply "enrollment" and it returns the `active_member` definition with its source system and rule. Ask about follow-up eligibility and it reports that your role cannot access that definition, rather than guessing at one.

Then show the audit trail — `call GET "/semantic/audit?limit=10" "$STEWARD"` — where those calls appear with `"via": "mcp"`. Every agent action is attributable.

---

## 8. The process pipeline, end to end

### 8.1 Components

```
AI agent (Claude Desktop / Inspector / any MCP client)
        │  MCP protocol  (stdio or streamable HTTP)
        ▼
mcp_server/     3 tools. No business logic. Forwards the caller's
                API key; adds X-Via: mcp. Never imports the app.
        │  HTTP
        ▼
app/            auth → audit → routing → resolver / rules → error envelope
        │
        ▼
catalog/concepts.yaml    the single source of truth, validated at startup
```

The MCP server is deliberately thin and never imports the application. It is a protocol adapter, nothing more, so the API remains the single place where meaning, access and audit are decided. An agent cannot obtain anything a direct API caller could not.

### 8.2 What happens on one request

1. **Startup.** The catalog is loaded and validated. Every problem is reported at once — unknown relationship targets, overlapping versions of one concept, dependency cycles, malformed rule expressions, an approved concept without an owner. If anything fails, the app refuses to start. A broken glossary never serves traffic.
2. **Authentication, before routing.** The API key is hashed and compared in constant time against every configured key with no early exit. Missing, wrong, malformed or duplicated key headers all produce the same `401` with the same body — so an unauthenticated caller cannot map the URL space or probe for valid keys.
3. **Resolution** (for `/resolve`). A pure function: normalise the term; match against ids, names, aliases and terms; drop drafts and versions not effective on the requested date; narrow by context if given; split the remainder into what this caller may see and what they may not. One visible candidate → `resolved`. Two or more → `ambiguous`. None visible but some exist → `restricted`, as a count only. No match → `not_found` with suggestions. **There is no code path that selects among multiple candidates.**
4. **Rule execution** (for `/evaluate`). The required facts are derived *statically* from the rule expression before anything runs, so short-circuit evaluation can never hide a missing fact. A fact that is absent is missing; a fact present as `null` is a legitimate value (an open-ended coverage end date). The rule is interpreted from typed data — no `eval`, no `exec`, no dynamic code execution anywhere.
5. **Errors.** Every failure, including framework validation errors, is mapped into the envelope. Validation errors keep only the field location and message, never echoing back what was sent.
6. **Audit.** One line per request, written after the response from allow-listed fields only — never by reading the request body. The `X-Via` header is mapped to `mcp` or `api`, never copied, so it cannot inject a forged line. Newlines in any value are escaped. Fact *names* are recorded, fact *values* never are.

### 8.3 The guarantees this pipeline provides

| Guarantee | How it is enforced |
|---|---|
| A rule is defined once | The YAML is the only place; documentation and execution read the same field |
| No guessing between meanings | No code path chooses among candidates; a mutation forcing one fails 13 tests |
| No LLM in the decision | Nothing in `app/` or `mcp_server/` imports any model client |
| No silent wrong answers | Missing facts return 422 naming them; an unmatched context warns instead of falling back |
| Errors are machine-readable | One envelope, correct status codes, no error ever returns 200 |
| Restricted content never leaks | Restricted concepts exist only as an integer count in the response type |
| Everything is attributable | Every request audited, including denials, with key labels never key values |
| Definitions have history | Versions with effective dates; `as_of` selects the version that applied |

---

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Server exits at startup | Invalid `config/api_keys.yaml` or an invalid catalog — the message names each problem |
| `401` on every call | Key missing, mistyped, or more than one `X-API-Key` header sent |
| `ModuleNotFoundError: app` | Run as a module (`python -m scripts.demo_conflict`), not as a file path |
| zsh "no matches found" | Quote any URL containing `?` |
| Claude Desktop shows no tools | Wrong absolute path, or a Python that is not the venv's; both must be absolute |
| MCP tool returns `unauthenticated` | The key never reached the server — check the environment variables or the header |

The audit log is the first place to look: it records every call with its outcome and status code.

---

## 10. Known limitations — state these before a client asks

1. **API keys stand in for identity.** Production needs OAuth per the current MCP authorization spec. Note that the current design forwards the caller's key, which that spec forbids for OAuth tokens; a real implementation needs the MCP server to hold its own credential plus token exchange to carry user identity. This is not a drop-in swap.
2. **Rule text and rule expression are kept in sync by stewards.** Both are stored; nothing automatically proves the prose matches the executable form.
3. **The catalog is in memory,** loaded from a file at startup. No persistence layer, no editing workflow, no approval process.
4. **Server errors are opaque to operators.** A 500 is caught, enveloped and audited, but its cause is never logged — safe, but unhelpful for debugging.
5. **The audit log has no rotation.**
6. **Dependency fact names appear in `missing_facts`,** which reveals a little about concepts a caller may not directly see.
7. **`/docs`, `/redoc` and `/openapi.json` are public.** Route shapes only; no catalog content.
8. **The alternative "hidden" visibility policy is not implemented.** Flipping it would touch about four places, not one.
9. **Windows is untested.** The code is cross-platform by construction and was verified on macOS.
10. **All data is synthetic.** The definitions are plausible but invented.
