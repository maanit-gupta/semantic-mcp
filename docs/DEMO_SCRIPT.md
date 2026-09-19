# Five-minute demo

**The story:** the same word gives different numbers → an agent asks about an ambiguous term → the service answers
with a clarifying question → context narrows it → a restricted concept is withheld from an analyst → every step is in
the audit trail → the eval proves the behaviour.

**Before you start:**
- Complete the setup in the [README](../README.md): venv activated, dependencies installed, and
  `config/api_keys.yaml` copied from the example.
- In terminal 1, start the API: `python -m uvicorn app.main:app --port 8000`.
- Run the commands below in terminal 2, with the venv activated.

**Verification status:**
- **bash:** every bash command was run on macOS on 2026-09-20. The outputs are pasted from those runs; long JSON is
  shortened where marked.
- **PowerShell:** these commands are *unverified*. PowerShell was not available to me. They need PowerShell 7 for
  `-SkipHttpErrorCheck`.
- **Dates:** `as_of` defaults to today's UTC date, so the date shown on your run will differ.

## 1. Same word, different numbers (60 s)

```bash
python -m scripts.demo_conflict
```

```
'member' on 2026-09-15 across 50 synthetic people (seeded, tests/fixtures/synthetic_people.py)

definition                   system             count
registered_member            crm                   47
active_member                enrollment            24
claimant_member              claims                22
reporting_month_member       analytics             32
currently_eligible_member    customer_service      23

Same person, different answer:
  P002: reporting_month_member yes, active_member no: enrolled during the reporting month, but coverage ended 2026-09-08
  P033: currently_eligible_member yes, active_member no: customer service still shows 'eligible', but coverage ended 2026-09-05
  P001: registered_member yes, active_member no: registered 2024-04-14, but coverage ended 2025-01-03
```

Say: five approved definitions, one population, five different answers to "how many members?". Every rule comes from
`catalog/concepts.yaml`, and the script uses the rule library directly: populations never go through the API.

## 2. The agent asks an ambiguous question (60 s)

The agent calls `resolve_term("member")`. The same request, sent straight to the API:

```bash
curl -s -X POST http://127.0.0.1:8000/semantic/resolve -H "X-API-Key: FAKE-analyst-key-for-local-demo-only" -H "Content-Type: application/json" -d '{"term":"member"}' | python -m json.tool
```

```powershell
$A = @{ "X-API-Key" = "FAKE-analyst-key-for-local-demo-only" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/semantic/resolve -Headers $A -ContentType "application/json" -Body '{"term":"member"}'
```

Output (shortened to the lines discussed):

```
    "status": "ambiguous",
            "id": "reporting_month_member",
            "id": "claimant_member",
            "id": "registered_member",
            "id": "currently_eligible_member",
            "id": "active_member",
    "clarifying_question": "Which meaning of 'member' do you need: analytics, claims, crm, customer_service or enrollment?",
    "restricted_count": 0,
```

Say: the service does not choose. The tool description tells the agent to ask the user this question and wait.

Through MCP (optional, *unverified in Inspector*): run `npx @modelcontextprotocol/inspector`, connect over stdio with
the command and environment in the README, and call `resolve_term` with `{"term": "member"}`. The same result comes
back as structured content. Step 6 runs this MCP path automatically.

## 3. Narrow by context (45 s)

```bash
curl -s -X POST http://127.0.0.1:8000/semantic/resolve -H "X-API-Key: FAKE-analyst-key-for-local-demo-only" -H "Content-Type: application/json" -d '{"term":"member","context":{"system":"enrollment"}}' | python -m json.tool
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/semantic/resolve -Headers $A -ContentType "application/json" -Body '{"term":"member","context":{"system":"enrollment"}}'
```

Output (first lines):

```
{
    "status": "resolved",
    "term": "member",
    "as_of": "2026-09-19",
    "concept": {
        "id": "active_member",
        "term": "member",
        "name": "Active member",
        "context": {
            "system": "enrollment",
            "domain": "eligibility"
        },
        "definition": "Person with coverage effective on the requested date",
        "aliases": [
            "enrolled member"
        ],
        "authoritative_source": {
            "system": "eligibility_enrollment_platform",
            "dataset": "coverage_spans"
        },
```

Further down: `"text": "coverage_start_date <= requested_date AND (coverage_end_date IS NULL OR coverage_end_date >= requested_date)"`,
plus version, owner and effective dates.

## 4. A restricted concept, as an analyst (45 s)

```bash
curl -s -X POST http://127.0.0.1:8000/semantic/resolve -H "X-API-Key: FAKE-analyst-key-for-local-demo-only" -H "Content-Type: application/json" -d '{"term":"eligible for follow up"}' | python -m json.tool
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/semantic/concepts/eligible_for_follow_up -H "X-API-Key: FAKE-analyst-key-for-local-demo-only"
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/semantic/resolve -Headers $A -ContentType "application/json" -Body '{"term":"eligible for follow up"}'
Invoke-WebRequest -Uri http://127.0.0.1:8000/semantic/concepts/eligible_for_follow_up -Headers $A -SkipHttpErrorCheck | Select-Object StatusCode, Content
```

```
{
    "status": "restricted",
    "term": "eligible for follow up",
    "as_of": "2026-09-19",
    "concept": null,
    "candidates": [],
    "clarifying_question": null,
    "suggestions": [],
    "restricted_count": 1,
    "warnings": [
        "1 meaning(s) of 'eligible for follow up' exist that your role cannot access"
    ]
}
{"error":{"code":"forbidden","message":"your role cannot access 'eligible_for_follow_up'","details":{"concept_id":"eligible_for_follow_up","required_role":["care_manager","steward"]}}}
HTTP 403
```

Say: the analyst learns that the concept exists and who may see it, never what it says. Repeat the command with
`FAKE-care-manager-key-for-local-demo-only` and the same term resolves in full.

## 5. The audit trail (30 s)

```bash
curl -s "http://127.0.0.1:8000/semantic/audit?limit=4" -H "X-API-Key: FAKE-steward-key-for-local-demo-only" | python -m json.tool
```

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/semantic/audit?limit=4" -Headers @{ "X-API-Key" = "FAKE-steward-key-for-local-demo-only" }
```

Output (shortened to role, endpoint, status and outcome of each entry, oldest first):

```
            "role": "analyst",
            "endpoint": "/semantic/resolve",
            "status_code": 200,
            "outcome": "ambiguous"
            "role": "analyst",
            "endpoint": "/semantic/resolve",
            "status_code": 200,
            "outcome": "ok"
            "role": "analyst",
            "endpoint": "/semantic/resolve",
            "status_code": 200,
            "outcome": "denied"
            "role": "analyst",
            "endpoint": "/semantic/concepts/{concept_id}",
            "status_code": 403,
            "outcome": "denied"
```

Say: every request is audited, including denials and requests with no key. Each line holds the key's label, never the
key itself. Calls made through MCP show `"via": "mcp"`. The audit endpoint is steward-only.

## 6. Prove it with the eval (30 s)

```bash
python -m evals.run
```

```
case                             path     expected     observed     concepts                                       result
------------------------------------------------------------------------------------------------------------------------
1-member-no-context              resolver ambiguous    ambiguous    reporting_month_member,claimant_member,registe PASS
2-member-enrollment              resolver resolved     resolved     active_member v1.0.0                           PASS
3-member-crm                     resolver resolved     resolved     registered_member v1.0.0                       PASS
4-member-customer-service        resolver resolved     resolved     currently_eligible_member v1.0.0               PASS
5-case-and-whitespace            resolver resolved     resolved     active_member v1.0.0                           PASS
6-member-unknown-context         resolver ambiguous    ambiguous    reporting_month_member,claimant_member,registe PASS
7a-follow-up-as-analyst          resolver restricted   restricted   -                                              PASS
7b-follow-up-as-care-manager     resolver resolved     resolved     eligible_for_follow_up v1.0.0                  PASS
8-misspelling                    resolver not_found    not_found    -                                              PASS
9a-reporting-month-v1            resolver resolved     resolved     reporting_month_member v1.0.0                  PASS
9b-reporting-month-v2            resolver resolved     resolved     reporting_month_member v2.0.0                  PASS
10-draft                         resolver not_found    not_found    -                                              PASS
11-deprecated                    resolver resolved     resolved     covered_life v1.0.0                            PASS
1-member-no-context              mcp      ambiguous    ambiguous    reporting_month_member,claimant_member,registe PASS
2-member-enrollment              mcp      resolved     resolved     active_member v1.0.0                           PASS
7a-follow-up-as-analyst          mcp      restricted   restricted   -                                              PASS
------------------------------------------------------------------------------------------------------------------------
16/16 checks passed (13 cases; 3 also via MCP)
```

The three `mcp` rows go through the MCP `resolve_term` tool and a real HTTP call to the API. The runner starts its own
API with throwaway keys, so it does not need terminal 1.

## If a step fails

- **Step 1 fails with `ModuleNotFoundError`:** run it from the repository root with the venv activated
  (`python -m scripts.demo_conflict`, not `python scripts/demo_conflict.py`).
- **Step 2 returns 401 `unauthenticated`:** `config/api_keys.yaml` is missing or has different keys. Copy it from
  `config/api_keys.example.yaml` and restart the API. The API refuses to start without a valid key file, so also
  check terminal 1 for the error.
- **Step 2 or 3 cannot connect:** the API is not running on port 8000. Start it in terminal 1, or change the port in
  the URLs.
