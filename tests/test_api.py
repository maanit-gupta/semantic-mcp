"""HTTP-level tests: every endpoint's success and error paths, plus the route-wide rule that every error is a non-200
response with the envelope.
"""
from datetime import date

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app, today_utc
from tests.conftest import headers
from tests.test_catalog import concept, fixture

A = headers("analyst")
CM = headers("care_manager")
ACTIVE_FACTS = {"coverage_start_date": "2026-01-01", "coverage_end_date": None}


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_health_is_public_and_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- GET /semantic/concepts -------------------------------------------------------------------------------------


def listed(client, query: str = "", hdrs=A) -> list[tuple[str, str]]:
    response = client.get(f"/semantic/concepts{query}", headers=hdrs)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["concepts"])
    return [(e["id"], e.get("version", "-")) for e in body["concepts"]]


def test_list_all_excludes_drafts_and_lists_each_version(client):
    assert listed(client) == [
        ("active_member", "1.0.0"),
        ("claimant_member", "1.0.0"),
        ("covered_life", "1.0.0"),
        ("currently_eligible_member", "1.0.0"),
        ("eligible_for_follow_up", "-"),  # restricted for analysts: existence only
        ("member_in_network", "1.0.0"),
        ("registered_member", "1.0.0"),
        ("reporting_month_member", "1.0.0"),
        ("reporting_month_member", "2.0.0"),
    ]


def test_list_for_care_manager_shows_restricted_in_full(client):
    assert ("eligible_for_follow_up", "1.0.0") in listed(client, hdrs=CM)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?term=member", ["active_member", "claimant_member", "currently_eligible_member", "registered_member",
                          "reporting_month_member", "reporting_month_member"]),
        ("?term=Enrolled-Member", ["active_member"]),
        ("?term=%20ACTIVE__member%20", ["active_member"]),
        ("?term=engaged%20member", []),  # draft
        ("?status=deprecated", ["covered_life"]),
        ("?system=analytics", ["reporting_month_member", "reporting_month_member"]),
        ("?system=CRM", ["registered_member"]),
        ("?system=care_management", ["eligible_for_follow_up"]),  # restricted entry, still filtered correctly
        ("?term=member&system=claims", ["claimant_member"]),
        ("?term=nothing", []),
    ],
)
def test_list_filters(client, query, expected):
    assert [cid for cid, _ in listed(client, query)] == expected


@pytest.mark.parametrize(
    "query",
    ["?status=draft", "?status=APPROVED", "?term=", "?system=", "?term=" + "m" * 201, "?system=" + "s" * 65],
)
def test_list_rejects_bad_filters(client, query):
    assert error(client.get(f"/semantic/concepts{query}", headers=A)) == (422, "validation_error")


# --- GET /semantic/concepts/{id} --------------------------------------------------------------------------------


def test_get_returns_full_definition(client):
    body = client.get("/semantic/concepts/active_member?as_of=2026-09-19", headers=A).json()
    assert body == {
        "as_of": "2026-09-19",
        "id": "active_member",
        "term": "member",
        "name": "Active member",
        "context": {"system": "enrollment", "domain": "eligibility"},
        "definition": "Person with coverage effective on the requested date",
        "aliases": ["enrolled member"],
        "authoritative_source": {"system": "eligibility_enrollment_platform", "dataset": "coverage_spans"},
        "source_systems": ["eligibility_enrollment_platform", "crm", "claims_system"],
        "rule": {
            "text": "coverage_start_date <= requested_date AND (coverage_end_date IS NULL OR coverage_end_date >= requested_date)",
            "expression": {
                "all": [
                    {"fact": "coverage_start_date", "op": "lte", "value": {"ref": "requested_date"}},
                    {"any": [
                        {"fact": "coverage_end_date", "op": "is_null"},
                        {"fact": "coverage_end_date", "op": "gte", "value": {"ref": "requested_date"}},
                    ]},
                ]
            },
        },
        "relationships": [{"type": "alternative_meaning_of", "target": "member", "description": "Enrollment meaning of member"}],
        "owner": "enrollment_data_stewardship",
        "version": "1.0.0",
        "status": "approved",
        "effective_from": "2024-01-01",
        "effective_to": None,
        "superseded_by": None,
        "allowed_roles": ["analyst", "care_manager", "steward"],
        "warnings": [],
    }


def test_get_defaults_as_of_to_utc_today(client):
    assert client.get("/semantic/concepts/active_member", headers=A).json()["as_of"] == today_utc().isoformat()


@pytest.mark.parametrize(
    ("as_of", "version"),
    [("2024-01-01", "1.0.0"), ("2025-12-31", "1.0.0"), ("2026-01-01", "2.0.0"), ("2030-06-01", "2.0.0")],
)
def test_get_selects_version_by_as_of(client, as_of, version):
    body = client.get(f"/semantic/concepts/reporting_month_member?as_of={as_of}", headers=A).json()
    assert (body["version"], body["as_of"]) == (version, as_of)


def test_get_deprecated_carries_warning(client):
    body = client.get("/semantic/concepts/covered_life", headers=A).json()
    assert (body["status"], body["superseded_by"], body["warnings"]) == (
        "deprecated", "active_member", ["'covered_life' is deprecated; use 'active_member' instead"]
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/semantic/concepts/engaged_member", (404, "not_found")),  # draft
        ("/semantic/concepts/no_such_concept", (404, "not_found")),
        ("/semantic/concepts/active_member?as_of=2023-12-31", (404, "not_effective")),
        ("/semantic/concepts/reporting_month_member?as_of=2023-06-01", (404, "not_effective")),
        ("/semantic/concepts/eligible_for_follow_up", (403, "forbidden")),
        ("/semantic/concepts/" + "x" * 101, (422, "validation_error")),
    ],
)
def test_get_errors(client, path, expected):
    assert error(client.get(path, headers=A)) == expected


def test_not_effective_details(client):
    body = client.get("/semantic/concepts/active_member?as_of=2023-12-31", headers=A).json()
    assert body["error"]["details"] == {"concept_id": "active_member", "as_of": "2023-12-31"}


@pytest.mark.parametrize(
    "bad", ["2026-13-01", "2026-02-30", "20260101", "2026/01/01", "1700000000", "2026-01-01T00:00:00", "", "today", "٢٠٢٦-٠١-٠١"]
)
def test_bad_as_of_is_422(client, bad):
    response = client.get("/semantic/concepts/active_member", params={"as_of": bad}, headers=A)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"errors": [{"loc": "query.as_of", "message": "expected a date as YYYY-MM-DD"}]}


# --- GET /semantic/concepts/{id}/relationships ------------------------------------------------------------------


def test_relationships_incoming_hides_restricted_description(client):
    body = client.get("/semantic/concepts/active_member/relationships?as_of=2026-09-19", headers=A).json()
    assert body == {
        "concept_id": "active_member",
        "version": "1.0.0",
        "as_of": "2026-09-19",
        "outgoing": [{"type": "alternative_meaning_of", "target": "member", "description": "Enrollment meaning of member"}],
        "incoming": [
            {"source": "eligible_for_follow_up", "type": "depends_on", "access": "restricted"},
            {"source": "member_in_network", "type": "depends_on", "description": "Must be an active member on the requested date"},
        ],
    }


def test_relationships_incoming_in_full_for_allowed_role(client):
    body = client.get("/semantic/concepts/active_member/relationships", headers=CM).json()
    assert body["incoming"][0] == {
        "source": "eligible_for_follow_up", "type": "depends_on", "description": "Must be an active member on the requested date"
    }


def test_relationships_outgoing(client):
    body = client.get("/semantic/concepts/member_in_network/relationships", headers=A).json()
    assert (body["outgoing"], body["incoming"]) == (
        [{"type": "depends_on", "target": "active_member", "description": "Must be an active member on the requested date"}],
        [],
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/semantic/concepts/eligible_for_follow_up/relationships", (403, "forbidden")),
        ("/semantic/concepts/engaged_member/relationships", (404, "not_found")),
        ("/semantic/concepts/no_such/relationships", (404, "not_found")),
        ("/semantic/concepts/active_member/relationships?as_of=2020-01-01", (404, "not_effective")),
        ("/semantic/concepts/active_member/relationships?as_of=bad", (422, "validation_error")),
    ],
)
def test_relationships_errors(client, path, expected):
    assert error(client.get(path, headers=A)) == expected


# --- POST /semantic/concepts/{id}/evaluate ----------------------------------------------------------------------


def evaluate(client, concept_id: str, body, hdrs=A):
    return client.post(f"/semantic/concepts/{concept_id}/evaluate", json=body, headers=hdrs)


def test_evaluate_response_fields(client):
    response = evaluate(client, "member_in_network",
                        {"requested_date": "2026-09-19", "facts": {**ACTIVE_FACTS, "network_status": "in_network"}})
    assert response.status_code == 200
    assert response.json() == {
        "concept_id": "member_in_network",
        "result": True,
        "rule_text": "network_status == 'in_network' AND active_member == TRUE",
        "source": {"system": "provider_network_directory", "dataset": "network_status"},
        "version": "1.0.0",
        "status": "approved",
        "requested_date": "2026-09-19",
        "warnings": [],
    }


def test_evaluate_false_is_a_200_answer(client):
    response = evaluate(client, "active_member", {"requested_date": "2025-06-01", "facts": ACTIVE_FACTS})
    assert (response.status_code, response.json()["result"]) == (200, False)


@pytest.mark.parametrize(("requested_date", "version", "result"), [("2025-06-30", "1.0.0", False), ("2026-06-30", "2.0.0", True)])
def test_evaluate_uses_version_effective_on_requested_date(client, requested_date, version, result):
    month = requested_date[:8]
    facts = {"coverage_start_date": month + "10", "coverage_end_date": None,
             "reporting_month_start": month + "01", "reporting_month_end": month + "30"}
    body = evaluate(client, "reporting_month_member", {"requested_date": requested_date, "facts": facts}).json()
    assert (body["version"], body["result"], body["rule_text"].split(" AND ")[0]) == (
        version, result, "coverage_start_date <= reporting_month_start" if version == "1.0.0" else "coverage_start_date <= reporting_month_end"
    )


def test_evaluate_deprecated_with_warning(client):
    body = evaluate(client, "covered_life", {"requested_date": "2026-09-19", "facts": {"coverage_start_date": "2020-01-01"}}).json()
    assert (body["result"], body["status"], body["warnings"]) == (
        True, "deprecated", ["'covered_life' is deprecated; use 'active_member' instead"]
    )


def test_evaluate_restricted_for_allowed_role(client):
    facts = {**ACTIVE_FACTS, "follow_up_eligible": True, "risk_flag": "high"}
    body = evaluate(client, "eligible_for_follow_up", {"requested_date": "2026-09-19", "facts": facts}, CM).json()
    assert (body["result"], body["version"]) == (False, "1.0.0")


def test_evaluate_dependency_is_evaluated_but_not_exposed(client):
    # member_in_network depends on active_member: the lapsed coverage makes it false, yet the response carries only
    # the target's fields, never the dependency's definition or rule.
    response = evaluate(client, "member_in_network", {"requested_date": "2026-09-19", "facts": {
        "coverage_start_date": "2025-01-01", "coverage_end_date": "2025-12-31", "network_status": "in_network"}})
    body = response.json()
    assert (body["result"], sorted(body)) == (
        False, ["concept_id", "requested_date", "result", "rule_text", "source", "status", "version", "warnings"]
    )
    assert "Person with coverage effective" not in response.text and "coverage_spans" not in response.text


@pytest.mark.parametrize(
    ("concept_id", "body", "expected", "details"),
    [
        ("active_member", {"requested_date": "2026-09-19", "facts": {"coverage_start_date": "2026-01-01"}},
         (422, "insufficient_context"), {"missing_facts": ["coverage_end_date"]}),
        ("member_in_network", {"requested_date": "2026-09-19", "facts": {"network_status": "out_of_network"}},
         (422, "insufficient_context"), {"missing_facts": ["coverage_end_date", "coverage_start_date"]}),
        ("active_member", {"requested_date": "2026-09-19", "facts": {"coverage_start_date": "2026-05-01", "coverage_end_date": "2026-04-01"}},
         (422, "invalid_facts"), {"errors": {"coverage_end_date": "must not be before coverage_start_date"}}),
        ("active_member", {"requested_date": "2026-09-19", "facts": {"coverage_start_date": "01/05/2026", "coverage_end_date": 5}},
         (422, "invalid_facts"), {"errors": {"coverage_end_date": "expected a date as YYYY-MM-DD", "coverage_start_date": "expected a date as YYYY-MM-DD"}}),
        ("engaged_member", {"requested_date": "2026-09-19", "facts": {}}, (404, "not_found"), {"concept_id": "engaged_member"}),
        ("no_such", {"requested_date": "2026-09-19", "facts": {}}, (404, "not_found"), {"concept_id": "no_such"}),
        ("active_member", {"requested_date": "2023-12-31", "facts": ACTIVE_FACTS}, (404, "not_effective"),
         {"concept_id": "active_member", "as_of": "2023-12-31"}),
        ("eligible_for_follow_up", {"requested_date": "2026-09-19", "facts": {}}, (403, "forbidden"),
         {"concept_id": "eligible_for_follow_up", "required_role": ["care_manager", "steward"]}),
    ],
)
def test_evaluate_errors(client, concept_id, body, expected, details):
    response = evaluate(client, concept_id, body)
    assert error(response) == expected
    assert response.json()["error"]["details"] == details


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"facts": {}}, "body.requested_date"),
        ({"requested_date": "2026-09-19"}, "body.facts"),
        ({"requested_date": "2026-13-45", "facts": {}}, "body.requested_date"),
        ({"requested_date": 20260919, "facts": {}}, "body.requested_date"),
        ({"requested_date": None, "facts": {}}, "body.requested_date"),
        ({"requested_date": "2026-09-19", "facts": []}, "body.facts"),
        ({"requested_date": "2026-09-19", "facts": "x"}, "body.facts"),
        ({"requested_date": "2026-09-19", "facts": {f"f{i}": 1 for i in range(101)}}, "body.facts"),
        ({"requested_date": "2026-09-19", "facts": {}, "extra": 1}, "body.extra"),
        ([], "body"),
        (None, "body"),
    ],
)
def test_evaluate_bad_body_is_422(client, body, loc):
    response = evaluate(client, "active_member", body)
    assert error(response) == (422, "validation_error")
    assert [e["loc"] for e in response.json()["error"]["details"]["errors"]] == [loc]


def test_evaluate_not_evaluable_paths(tmp_path, audit_path):
    # A dependency with no version on the date, and a deprecated concept without a rule (neither exists in the seed).
    data = fixture()
    concept(data, "base")["effective_from"] = "2024-06-01"
    concept(data, "derived")["allowed_roles"] = ["analyst"]
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    client = TestClient(create_app(catalog_path=catalog_file, audit_path=audit_path), raise_server_exceptions=False)
    dep = evaluate(client, "derived", {"requested_date": "2024-03-01", "facts": {"start": "2024-01-01", "status": "ok"}})
    norule = evaluate(client, "legacy", {"requested_date": "2024-03-01", "facts": {}})
    assert (error(dep), dep.json()["error"]["details"]) == ((422, "not_evaluable"), {"concept_id": "derived"})
    assert dep.json()["error"]["message"] == "concept 'base' has no version effective on 2024-03-01"
    assert (error(norule), norule.json()["error"]["message"]) == ((422, "not_evaluable"), "concept 'legacy' v1.0.0 has no rule to evaluate")


def test_evaluate_uses_date_not_today(client):
    # requested_date is honoured even far from today; nothing falls back to the current date.
    body = evaluate(client, "active_member", {"requested_date": "2030-01-01", "facts": ACTIVE_FACTS}).json()
    assert (body["requested_date"], body["result"]) == ("2030-01-01", True)
    assert date.fromisoformat(body["requested_date"]) != today_utc()


# --- Route-wide: every error is non-200 with the envelope -------------------------------------------------------

ERROR_CASES = [
    # (method, path, headers, json)
    ("GET", "/semantic/concepts", {}, None),
    ("GET", "/semantic/concepts?status=draft", A, None),
    ("GET", "/semantic/concepts/no_such", A, None),
    ("GET", "/semantic/concepts/active_member?as_of=2020-01-01", A, None),
    ("GET", "/semantic/concepts/active_member?as_of=x", A, None),
    ("GET", "/semantic/concepts/eligible_for_follow_up", A, None),
    ("GET", "/semantic/concepts/active_member", {}, None),
    ("GET", "/semantic/concepts/no_such/relationships", A, None),
    ("GET", "/semantic/concepts/eligible_for_follow_up/relationships", A, None),
    ("GET", "/semantic/concepts/active_member/relationships", {}, None),
    ("POST", "/semantic/concepts/active_member/evaluate", A, {}),
    ("POST", "/semantic/concepts/active_member/evaluate", A, {"requested_date": "2026-01-01", "facts": {}}),
    ("POST", "/semantic/concepts/active_member/evaluate", A, {"requested_date": "2026-01-01", "facts": {"coverage_start_date": "x", "coverage_end_date": None}}),
    ("POST", "/semantic/concepts/engaged_member/evaluate", A, {"requested_date": "2026-01-01", "facts": {}}),
    ("POST", "/semantic/concepts/eligible_for_follow_up/evaluate", A, {"requested_date": "2026-01-01", "facts": {}}),
    ("POST", "/semantic/concepts/active_member/evaluate", {}, {"requested_date": "2026-01-01", "facts": {}}),
    ("GET", "/semantic/audit", A, None),
    ("GET", "/semantic/audit?limit=0", headers("steward"), None),
    ("GET", "/semantic/audit", {}, None),
    ("DELETE", "/semantic/concepts/active_member", A, None),
    ("PUT", "/semantic/concepts/active_member", A, {}),
    ("PATCH", "/semantic/concepts", A, {}),
    ("GET", "/semantic/concepts/active_member/evaluate", A, None),
    ("POST", "/semantic/concepts", A, {}),
    ("GET", "/semantic/nope", A, None),
    ("GET", "/nope", A, None),
    ("POST", "/health", {}, None),
    ("POST", "/semantic/resolve", {}, {"term": "member"}),
    ("POST", "/semantic/resolve", A, {}),
    ("POST", "/semantic/resolve", A, {"term": "   "}),
    ("POST", "/semantic/resolve", A, {"term": "member", "as_of": "2026-02-30"}),
    ("POST", "/semantic/resolve", A, {"term": "member", "context": "enrollment"}),
    ("GET", "/semantic/resolve", A, None),
]


@pytest.mark.parametrize(("method", "path", "hdrs", "body"), ERROR_CASES)
def test_every_error_is_non_200_with_envelope(client, method, path, hdrs, body):
    response = client.request(method, path, headers=hdrs, json=body)
    assert response.status_code in (401, 403, 404, 405, 422)
    payload = response.json()
    assert list(payload) == ["error"]
    assert sorted(payload["error"]) == ["code", "details", "message"]
    assert isinstance(payload["error"]["code"], str) and isinstance(payload["error"]["details"], dict)
    assert response.headers["content-type"] == "application/json"


def test_no_write_routes_exist(client):
    # Read-only service: the only non-GET routes are POST resolve and evaluate, which compute and store nothing.
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", set()) if m not in ("GET", "HEAD")}
    assert routes == {("POST", "/semantic/concepts/{concept_id}/evaluate"), ("POST", "/semantic/resolve")}


# --- POST /semantic/resolve -------------------------------------------------------------------------------------


def resolve(client, body, hdrs=A):
    return client.post("/semantic/resolve", json=body, headers=hdrs)


def test_resolve_ambiguous_response_shape(client):
    response = resolve(client, {"term": "member", "as_of": "2026-09-19"})
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["status", "term", "as_of", "concept", "candidates", "clarifying_question", "suggestions",
                          "restricted_count", "warnings"]
    assert (body["status"], body["term"], body["as_of"], body["concept"], body["suggestions"], body["restricted_count"],
            body["warnings"]) == ("ambiguous", "member", "2026-09-19", None, [], 0, [])
    assert [c["id"] for c in body["candidates"]] == [
        "reporting_month_member", "claimant_member", "registered_member", "currently_eligible_member", "active_member"
    ]
    assert body["candidates"][4] == {
        "id": "active_member", "name": "Active member", "term": "member",
        "context": {"system": "enrollment", "domain": "eligibility"},
        "definition": "Person with coverage effective on the requested date", "version": "1.0.0", "status": "approved",
    }
    assert body["clarifying_question"] == (
        "Which meaning of 'member' do you need: analytics, claims, crm, customer_service or enrollment?"
    )


def test_resolve_resolved_carries_full_definition(client):
    body = resolve(client, {"term": "member", "context": {"system": "enrollment"}, "as_of": "2026-09-19"}).json()
    full = client.get("/semantic/concepts/active_member?as_of=2026-09-19", headers=A).json()
    assert (body["status"], body["candidates"], body["clarifying_question"]) == ("resolved", [], None)
    assert body["concept"] == {k: v for k, v in full.items() if k != "as_of"}


def test_resolve_defaults_as_of_to_utc_today(client):
    assert resolve(client, {"term": "member"}).json()["as_of"] == today_utc().isoformat()


@pytest.mark.parametrize(
    ("body", "hdrs", "status", "concept", "restricted_count"),
    [
        ({"term": "member", "context": {"system": "crm"}}, A, "resolved", "registered_member", 0),
        ({"term": "member", "context": {"system": "customer_service"}}, A, "resolved", "currently_eligible_member", 0),
        ({"term": "Active  Member"}, A, "resolved", "active_member", 0),
        ({"term": "eligible for follow up"}, A, "restricted", None, 1),
        ({"term": "eligible for follow up"}, CM, "resolved", "eligible_for_follow_up", 0),
        ({"term": "covered life"}, A, "resolved", "covered_life", 0),
        ({"term": "engaged member"}, A, "not_found", None, 0),
        ({"term": "member", "context": None}, A, "ambiguous", None, 0),
        ({"term": "member", "context": {"system": None}}, A, "ambiguous", None, 0),
        ({"term": "member", "as_of": None}, A, "ambiguous", None, 0),
    ],
)
def test_resolve_outcomes(client, body, hdrs, status, concept, restricted_count):
    result = resolve(client, body, hdrs).json()
    assert (result["status"], (result["concept"] or {}).get("id"), result["restricted_count"]) == (status, concept, restricted_count)


@pytest.mark.parametrize(("as_of", "version"), [("2025-12-31", "1.0.0"), ("2026-01-01", "2.0.0")])
def test_resolve_version_by_as_of(client, as_of, version):
    body = resolve(client, {"term": "reporting month member", "as_of": as_of}).json()
    assert (body["status"], body["concept"]["version"]) == ("resolved", version)


def test_resolve_not_found_with_suggestion(client):
    body = resolve(client, {"term": "membr"}).json()
    assert (body["status"], body["suggestions"], body["candidates"]) == ("not_found", ["member"], [])


@pytest.mark.parametrize(("body", "outcome"), [
    ({"term": "member", "context": {"system": "enrollment"}}, "ok"),
    ({"term": "member"}, "ambiguous"),
    ({"term": "membr"}, "not_found"),
    ({"term": "eligible for follow up"}, "denied"),
])
def test_resolve_audit_outcome(client, audit_path, body, outcome):
    import json

    resolve(client, body)
    (entry,) = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert (entry["endpoint"], entry["status_code"], entry["outcome"], entry["params"]["term"]) == (
        "/semantic/resolve", 200, outcome, body["term"]
    )


RESTRICTED_STRINGS = [
    "eligible_for_follow_up", "Eligible for follow up", "outreach eligible",
    "Active member eligible for outreach based on plan status and care gap follow-up rules", "care_gap_follow_up",
]


@pytest.mark.parametrize(
    "body",
    [
        {"term": "member"}, {"term": "membr"}, {"term": "eligible"}, {"term": "outreach"}, {"term": "follow up"},
        {"term": "outreach eligble"}, {"term": "member", "context": {"system": "care_management"}},
        {"term": "member", "context": {"domain": "outreach"}}, {"term": "active member", "context": {"system": "care_management"}},
    ],
)
def test_analyst_resolve_never_leaks_restricted_content(client, body):
    text = resolve(client, body).text
    for secret in RESTRICTED_STRINGS:
        assert secret not in text


def test_analyst_restricted_resolution_has_count_only(client):
    body = resolve(client, {"term": "eligible for follow up"}).json()
    assert body == {
        "status": "restricted", "term": "eligible for follow up", "as_of": today_utc().isoformat(), "concept": None,
        "candidates": [], "clarifying_question": None, "suggestions": [], "restricted_count": 1,
        "warnings": ["1 meaning(s) of 'eligible for follow up' exist that your role cannot access"],
    }
    assert "Active member eligible for outreach" not in str(body)


# --- Hostile inputs: never a 5xx --------------------------------------------------------------------------------

HOSTILE_RESOLVE = [
    # (raw body or json, content-type, expected status)
    ({}, None, 422),
    ({"term": ""}, None, 422),
    ({"term": "   "}, None, 422),
    ({"term": "_-_"}, None, 422),
    ({"term": "m" * 201}, None, 422),
    ({"term": "m" * 200}, None, 200),
    ({"term": "m" * 1_000_000}, None, 422),
    ({"term": None}, None, 422),
    ({"term": 5}, None, 422),
    ({"term": True}, None, 422),
    ({"term": ["member"]}, None, 422),
    ({"term": {"$ne": ""}}, None, 422),
    ({"term": "membre ü 名前 🙂"}, None, 200),
    ({"term": "ＭＥＭＢＥＲ"}, None, 200),
    ({"term": "member\u0000"}, None, 200),
    ({"term": "member", "context": "enrollment"}, None, 422),
    ({"term": "member", "context": []}, None, 422),
    ({"term": "member", "context": {"system": 5}}, None, 422),
    ({"term": "member", "context": {"system": ""}}, None, 422),
    ({"term": "member", "context": {"system": "   "}}, None, 422),
    ({"term": "member", "context": {"system": "x" * 65}}, None, 422),
    ({"term": "member", "context": {"system": {"$gt": ""}}}, None, 422),
    ({"term": "member", "context": {"foo": 1}}, None, 422),
    ({"term": "member", "context": {"system": "enrollment", "role": "steward"}}, None, 422),
    ({"term": "member", "as_of": "2026-02-30"}, None, 422),
    ({"term": "member", "as_of": 20260101}, None, 422),
    ({"term": "member", "as_of": "2026-01-01T00:00:00Z"}, None, 422),
    ({"term": "member", "as_of": "0000-01-01"}, None, 422),
    ({"term": "member", "as_of": "9999-12-31"}, None, 200),
    ({"term": "member", "role": "steward"}, None, 422),
    ([], None, 422),
    ("member", None, 422),
    (None, None, 422),
    (b"", "application/json", 422),
    (b"{not json", "application/json", 422),
    (rb'{"term": "\ud800"}', "application/json", 422),  # lone surrogate: Pydantic rejects it
    (rb'{"term": "a\ud83d\ude42"}', "application/json", 200),  # valid pair: rendered as ASCII escapes
    (b'{"term": "member"}', "text/plain", 422),
    (b'{"term": "member", "term": 5}', "application/json", 422),
    (b"\xff\xfe\x00", "application/json", 400),  # not UTF-8: FastAPI's parse error, enveloped
    (b'{"term": ' + b"[" * 100_000 + b"]" * 100_000 + b"}", "application/json", 400),
]


@pytest.mark.parametrize(("body", "content_type", "expected"), HOSTILE_RESOLVE)
def test_hostile_resolve_inputs_never_5xx(client, body, content_type, expected):
    if isinstance(body, bytes):
        response = client.post("/semantic/resolve", content=body, headers={**A, "Content-Type": content_type})
    else:
        response = client.post("/semantic/resolve", json=body, headers=A)
    assert response.status_code == expected
    if expected != 200:
        assert sorted(response.json()["error"]) == ["code", "details", "message"]


@pytest.mark.parametrize(
    "path",
    [
        "/semantic/concepts?term=%00", "/semantic/concepts?term=%F0%9F%99%82", "/semantic/concepts?system=%0A%0D",
        "/semantic/concepts/%00", "/semantic/concepts/..%2F..%2Fetc%2Fpasswd", "/semantic/concepts/%F0%9F%99%82",
        "/semantic/concepts/active_member?as_of=%00", "/semantic/concepts/active_member?as_of=2026-01-01&as_of=x",
        "/semantic/concepts/active_member/relationships?as_of=99999-01-01", "/semantic/audit?limit=99999999999999999999",
    ],
)
def test_hostile_query_and_path_inputs_never_5xx(client, path):
    response = client.get(path, headers=headers("steward"))
    assert response.status_code < 500
    assert response.status_code == 200 or sorted(response.json()["error"]) == ["code", "details", "message"]


def test_hostile_evaluate_inputs_never_5xx(client):
    bodies = [
        {"requested_date": "2026-01-01", "facts": {"coverage_start_date": {"$gt": ""}, "coverage_end_date": [None]}},
        {"requested_date": "2026-01-01", "facts": {"coverage_start_date": "9999-12-31", "coverage_end_date": "0001-01-01"}},
        {"requested_date": "2026-01-01", "facts": {"coverage_start_date": 1e308, "coverage_end_date": -0.0}},
        {"requested_date": "2026-01-01", "facts": {"": 1, "\u0000": 2}},
    ]
    statuses = [evaluate(client, "active_member", b).status_code for b in bodies]
    assert statuses == [422, 422, 422, 422]
