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
    # Read-only service: the only non-GET route is POST evaluate (it computes, it does not store).
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", set()) if m not in ("GET", "HEAD")}
    assert routes == {("POST", "/semantic/concepts/{concept_id}/evaluate")}
