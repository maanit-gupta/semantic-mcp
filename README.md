# Ryan-MCP: semantic glossary API and MCP server

A read-only glossary service for business terms such as **"member"**. Different systems use the word differently
(CRM, enrollment, claims, analytics, customer service). The service returns the approved meaning for the caller's
context: definition, authoritative source, rule, relationships, version, owner and effective dates. When the context
does not pick exactly one meaning, it returns the candidates and a clarifying question instead of guessing.

It is a proof of concept with **synthetic data only**. It returns metadata, never data about people. Resolution is
deterministic code; there is no LLM in the service or the MCP server.

- `app/`: FastAPI service. API-key auth, audit log, concept endpoints, rule evaluator, resolver.
- `mcp_server/`: a thin MCP server with three tools. It calls the API over HTTP and holds no business logic.
- `catalog/concepts.yaml`: the concepts and their rules, validated when the app starts.

Design decisions, assumptions and limitations: [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md). Five-minute walkthrough:
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md). Build log: [PROGRESS.md](PROGRESS.md).

## Architecture

```
AI agent (Claude Desktop, MCP Inspector, ...)
        |  MCP: stdio (local) or Streamable HTTP
        v
mcp_server/server.py   3 tools; forwards the caller's API key; sends X-Via: mcp
        |  HTTP + X-API-Key
        v
app/                   auth + audit middleware -> resolver / concepts / evaluator
        |
        v
catalog/concepts.yaml  loaded and validated at startup; the app will not start if it is invalid
```

Every rule is written once, in `catalog/concepts.yaml`, as a declarative expression that `app/rules.py` executes
(no `eval`). The hand-written `rule.text` next to it is for people.

## Setup

Python 3.11. Commands run from the repository root.

Linux/macOS (bash):

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp config/api_keys.example.yaml config/api_keys.yaml
```

Windows (PowerShell), *unverified: written for Windows, not run on it*:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item config\api_keys.example.yaml config\api_keys.yaml
```

`requirements.txt` holds the runtime packages (FastAPI, uvicorn, Pydantic, PyYAML, `mcp`, `httpx2`);
`requirements-dev.txt` adds pytest.

### API keys (a demo stand-in for identity)

Each key maps to a role: `analyst`, `care_manager` or `steward`. The example file has obviously fake keys such as
`FAKE-analyst-key-for-local-demo-only`. Replace them in your copy, which git ignores. To keep keys outside the
repository, point `SEMANTIC_API_KEYS_FILE` at another file. The app refuses to start if the file is missing or
invalid, and the error names the problem without printing any key.

**API keys are a stand-in for real identity.** Production would use OAuth 2.1 as described in the MCP authorization
specification; [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) describes the gap.

| Environment variable | Used by | Default |
|---|---|---|
| `SEMANTIC_API_KEYS_FILE` | API | `config/api_keys.yaml` |
| `SEMANTIC_AUDIT_LOG` | API | `audit/audit.jsonl` (git-ignored) |
| `SEMANTIC_API_URL` | MCP server | `http://127.0.0.1:8000` |
| `SEMANTIC_API_KEY` | MCP server, stdio only | none (every call is then 401) |

## Run the API

```bash
python -m uvicorn app.main:app --port 8000
```

`/health`, `/docs`, `/redoc` and `/openapi.json` are public. Everything else needs an `X-API-Key` header:

```bash
curl -s -X POST http://127.0.0.1:8000/semantic/resolve -H "X-API-Key: FAKE-analyst-key-for-local-demo-only" -H "Content-Type: application/json" -d '{"term":"member"}'
```

The response has `"status":"ambiguous"`, five candidates and
`"clarifying_question":"Which meaning of 'member' do you need: analytics, claims, crm, customer_service or enrollment?"`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (public) |
| GET | `/semantic/concepts?term=&status=&system=` | List concepts; restricted ones show only id, name, term |
| GET | `/semantic/concepts/{id}?as_of=` | Full definition of the version effective on `as_of` |
| GET | `/semantic/concepts/{id}/relationships?as_of=` | Outgoing and incoming relationships |
| POST | `/semantic/resolve` | `{term, context?: {system?, domain?}, as_of?}` → resolved / ambiguous / not_found / restricted |
| POST | `/semantic/concepts/{id}/evaluate` | `{requested_date, facts}` → true/false, with rule text, source and version |
| GET | `/semantic/audit?limit=` | Recent audit lines (steward only) |

Every error is `{"error": {"code", "message", "details"}}` with a 4xx/5xx status. Dates are `YYYY-MM-DD`; a missing
`as_of` means today's UTC date.

## Run the MCP server

The MCP server needs the API running.

- **stdio** (for MCP Inspector and Claude Desktop): `python -m mcp_server.server`. The key comes from
  `SEMANTIC_API_KEY`.
- **Streamable HTTP**: `python -m mcp_server.server --transport http --port 8001`, endpoint
  `http://127.0.0.1:8001/mcp`. Each caller sends its own `X-API-Key` header, which is forwarded to the API. The server
  never uses its own key for HTTP callers. The SDK answers only `localhost` hosts by default.

| Tool | Wraps | Use |
|---|---|---|
| `resolve_term(term, context?, as_of?)` | `POST /semantic/resolve` | Call first for any business term; if `ambiguous`, ask the user the `clarifying_question` |
| `get_definition(concept_id, as_of?)` | `GET /concepts/{id}` + `/relationships` | Full definition of a known concept id |
| `evaluate_concept(concept_id, requested_date, facts)` | `POST /concepts/{id}/evaluate` | Apply a rule to facts the user supplies |

Results are the API's JSON as structured content. API errors come back as MCP error results (`is_error`) carrying the
API's error envelope. Every call is audited with `via: "mcp"`. The SDK is pinned: `mcp==2.2.0`.

### MCP Inspector and Claude Desktop (Windows)

*Unverified: neither tool, nor Windows, was available when this was written.* The same launch was verified on macOS:
the venv's Python, `-m mcp_server.server`, `PYTHONPATH` pointing at the repository, and a different working directory.
Replace `C:\path\to\semantic-mcp` with your clone, and keep the API running (`python -m uvicorn app.main:app --port 8000`).

MCP Inspector over stdio (needs Node.js):

```powershell
$env:PYTHONPATH = "C:\path\to\semantic-mcp"
$env:SEMANTIC_API_URL = "http://127.0.0.1:8000"
$env:SEMANTIC_API_KEY = "FAKE-analyst-key-for-local-demo-only"
npx @modelcontextprotocol/inspector C:\path\to\semantic-mcp\.venv\Scripts\python.exe -m mcp_server.server
```

For Streamable HTTP, start `python -m mcp_server.server --transport http --port 8001`, then in the Inspector choose
"Streamable HTTP", enter `http://127.0.0.1:8001/mcp`, and add the header `X-API-Key: FAKE-analyst-key-for-local-demo-only`.

Claude Desktop: add to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "ryan-mcp-glossary": {
      "command": "C:\\path\\to\\semantic-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\semantic-mcp",
        "SEMANTIC_API_URL": "http://127.0.0.1:8000",
        "SEMANTIC_API_KEY": "FAKE-analyst-key-for-local-demo-only"
      }
    }
  }
}
```

To see the restricted concept resolve, use `FAKE-care-manager-key-for-local-demo-only` instead.

## Tests, eval and demo

```bash
python -m pytest -q
python -m evals.run
python -m scripts.demo_conflict
```

- The tests start real API and MCP server processes on free local ports. They use `tests/fixtures/api_keys.yaml`,
  not your key file.
- `evals.run` runs 13 ambiguous-question cases through the resolver and the three core cases through the MCP tool. It
  prints a pass/fail table and exits non-zero on any failure. It needs no running server and no key file.
- `demo_conflict` evaluates the five `member` definitions over 50 seeded synthetic people and prints differing counts.
  It uses the rule library directly, never the API.

## Limitations

- **API keys** stand in for identity; production needs OAuth (see ASSUMPTIONS).
- **Keeping `rule.text` in sync with `rule.expression`** is a stewardship task. Nothing checks that the English
  matches the logic.
- **The catalog is in memory**, loaded from YAML at startup. There is no database, no write API and no approval
  workflow. Changes are edits to the file plus a restart.
- **Metadata only.** The service holds no member data; `evaluate` works only on facts the caller sends.
- **The audit log** is one local JSONL file for one process, with no rotation.
- **The MCP server forwards the caller's API key** to the API. That is acceptable for this key-based stand-in, but the
  MCP authorization spec forbids passing OAuth tokens through (see ASSUMPTIONS).
