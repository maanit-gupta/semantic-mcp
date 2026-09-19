"""Audit log: a line for success, denial, unauthenticated and error requests; no key material anywhere; newlines in
caller input cannot forge a second line; concurrent writes stay whole. Includes the key-leak test.
"""
import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app import rules
from app.audit import AuditLog, outcome_for_status
from app.main import create_app
from tests.conftest import headers

FIELDS = ["endpoint", "error_code", "key_label", "method", "outcome", "params", "role", "status_code", "ts", "via"]


def lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def without_ts(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "ts"}


def test_success_line(client, audit_path):
    client.get("/semantic/concepts/active_member?as_of=2026-01-01", headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert sorted(entry) == FIELDS
    assert without_ts(entry) == {
        "key_label": "test-analyst", "role": "analyst", "via": "api", "method": "GET",
        "endpoint": "/semantic/concepts/{concept_id}", "params": {"concept_id": "active_member", "as_of": "2026-01-01"},
        "status_code": 200, "error_code": None, "outcome": "ok",
    }


def test_denied_line(client, audit_path):
    client.get("/semantic/concepts/eligible_for_follow_up?as_of=2026-01-01", headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert without_ts(entry) == {
        "key_label": "test-analyst", "role": "analyst", "via": "api", "method": "GET",
        "endpoint": "/semantic/concepts/{concept_id}", "params": {"concept_id": "eligible_for_follow_up", "as_of": "2026-01-01"},
        "status_code": 403, "error_code": "forbidden", "outcome": "denied",
    }


def test_unauthenticated_line(client, audit_path):
    client.get("/semantic/concepts/active_member", headers={"X-API-Key": "wrong-key-0000000000"})
    client.get("/semantic/no-such-route")
    first, second = lines(audit_path)
    assert without_ts(first) == {
        "key_label": None, "role": None, "via": "api", "method": "GET", "endpoint": "/semantic/concepts/active_member",
        "params": {}, "status_code": 401, "error_code": "unauthenticated", "outcome": "unauthenticated",
    }
    assert (second["endpoint"], second["outcome"]) == ("/semantic/no-such-route", "unauthenticated")


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "error_code", "outcome"),
    [
        ("GET", "/semantic/concepts/no_such", None, 404, "not_found", "not_found"),
        ("GET", "/semantic/concepts/active_member?as_of=2020-01-01", None, 404, "not_effective", "not_found"),
        ("GET", "/semantic/concepts/active_member?as_of=bad", None, 422, "validation_error", "error"),
        ("POST", "/semantic/concepts/active_member/evaluate", {"requested_date": "2026-01-01", "facts": {}}, 422, "insufficient_context", "error"),
        ("DELETE", "/semantic/concepts", None, 405, "method_not_allowed", "error"),
        ("GET", "/semantic/no-such-route", None, 404, "not_found", "not_found"),
        ("GET", "/health", None, 200, None, "ok"),
    ],
)
def test_outcome_and_error_code_per_status(client, audit_path, method, path, body, status, error_code, outcome):
    response = client.request(method, path, json=body, headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert (response.status_code, entry["status_code"], entry["error_code"], entry["outcome"]) == (status, status, error_code, outcome)


def test_unhandled_exception_is_a_500_envelope_and_audited(client, audit_path, monkeypatch, caplog, capfd):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(rules, "evaluate", boom)
    caplog.set_level(logging.DEBUG)
    body = {"requested_date": "2026-01-01", "facts": {}}
    response = client.post("/semantic/concepts/active_member/evaluate", json=body, headers=headers("analyst"))
    assert (response.status_code, response.json()) == (
        500, {"error": {"code": "internal_error", "message": "internal error", "details": {}}}
    )
    assert "secret internal detail" not in response.text
    (entry,) = lines(audit_path)
    assert (entry["status_code"], entry["error_code"], entry["outcome"]) == (500, "internal_error", "error")
    # The cause is not logged anywhere (documented limitation): not in log records, output, or the audit line.
    assert "secret internal detail" not in "".join(r.getMessage() for r in caplog.records) + "".join(capfd.readouterr())
    assert "secret internal detail" not in audit_path.read_text(encoding="utf-8")


def test_rejected_request_is_audited_with_empty_params(client, audit_path):
    # Validation fails before the route runs, and only routes fill in params (documented in ASSUMPTIONS.md).
    client.post("/semantic/concepts/active_member/evaluate", json={"requested_date": "bad", "facts": {}}, headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert (entry["params"], entry["endpoint"], entry["error_code"], entry["role"]) == (
        {}, "/semantic/concepts/{concept_id}/evaluate", "validation_error", "analyst"
    )


def test_evaluate_logs_fact_names_not_values(client, audit_path):
    facts = {"coverage_start_date": "2026-01-01", "coverage_end_date": None, "zz_note": "PERSON-VALUE-XYZ"}
    client.post("/semantic/concepts/active_member/evaluate", json={"requested_date": "2026-09-19", "facts": facts}, headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert entry["params"] == {
        "concept_id": "active_member", "requested_date": "2026-09-19",
        "fact_names": ["coverage_end_date", "coverage_start_date", "zz_note"],
    }
    assert "PERSON-VALUE-XYZ" not in audit_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("value", "expected"), [("mcp", "mcp"), ("MCP", "api"), ("mcp ", "api"), ("anything", "api"), (None, "api")])
def test_via_is_mapped_never_copied(client, audit_path, value, expected):
    extra = {"X-Via": value} if value is not None else {}
    client.get("/semantic/concepts", headers=headers("analyst", **extra))
    (entry,) = lines(audit_path)
    assert entry["via"] == expected


def test_newlines_in_caller_input_cannot_forge_a_line(client, audit_path):
    forged = '\n{"key_label": "test-steward", "role": "steward", "outcome": "ok"}\n'
    client.get("/semantic/concepts", params={"term": "member" + forged}, headers=headers("analyst"))
    client.get("/semantic/concepts/x%0A%7B%22role%22%3A%22steward%22%7D", headers=headers("analyst"))
    raw = audit_path.read_text(encoding="utf-8")
    assert raw.count("\n") == 2
    first, second = lines(audit_path)
    assert (first["role"], first["params"]["term"]) == ("analyst", "member" + forged)
    assert (second["role"], second["params"]["concept_id"]) == ("analyst", 'x\n{"role":"steward"}')


def test_writer_escapes_line_separators(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.write({"params": {"term": "a\nb\rc\u2028d\u2029e"}})
    raw = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert raw.count("\n") == 1 and "\r" not in raw and "\u2028" not in raw
    assert json.loads(raw)["params"]["term"] == "a\nb\rc\u2028d\u2029e"


def test_long_params_are_clipped(client, audit_path):
    client.get("/semantic/concepts", params={"term": "m" * 200}, headers=headers("analyst"))
    (entry,) = lines(audit_path)
    assert entry["params"]["term"] == "m" * 200
    client.post("/semantic/concepts/active_member/evaluate",
                json={"requested_date": "2026-09-19", "facts": {f"f{i:03}" + "x" * 300: 1 for i in range(80)}},
                headers=headers("analyst"))
    names = lines(audit_path)[1]["params"]["fact_names"]
    assert (len(names), {len(n) for n in names}) == (50, {200})


def test_concurrent_writes_stay_whole(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")

    def worker(n: int) -> None:
        for i in range(50):
            log.write({"params": {"worker": n, "i": i, "pad": "x" * 500}})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = lines(tmp_path / "a.jsonl")
    assert len(entries) == 400
    assert sorted((e["params"]["worker"], e["params"]["i"]) for e in entries) == [(n, i) for n in range(8) for i in range(50)]


def test_one_line_per_request(client, audit_path):
    for _ in range(3):
        client.get("/semantic/concepts", headers=headers("analyst"))
    client.get("/semantic/concepts")
    client.get("/health")
    assert [e["outcome"] for e in lines(audit_path)] == ["ok", "ok", "ok", "unauthenticated", "ok"]


def test_outcome_vocabulary():
    assert [outcome_for_status(s) for s in (200, 400, 401, 403, 404, 405, 422, 500)] == [
        "ok", "error", "unauthenticated", "denied", "not_found", "error", "error", "error"
    ]


# --- GET /semantic/audit ----------------------------------------------------------------------------------------


def test_audit_endpoint_is_steward_only_and_its_own_access_is_audited(client, audit_path):
    client.get("/semantic/concepts", headers=headers("analyst"))
    denied = client.get("/semantic/audit", headers=headers("care_manager"))
    allowed = client.get("/semantic/audit?limit=2", headers=headers("steward"))
    assert denied.status_code == 403
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["count"] == 2
    assert [(e["role"], e["endpoint"], e["outcome"]) for e in body["entries"]] == [
        ("analyst", "/semantic/concepts", "ok"),
        ("care_manager", "/semantic/audit", "denied"),
    ]
    last = lines(audit_path)[-1]
    assert (last["role"], last["endpoint"], last["params"], last["outcome"]) == ("steward", "/semantic/audit", {"limit": 2}, "ok")


@pytest.mark.parametrize("limit", ["0", "1001", "-1", "abc", "1e3"])
def test_audit_limit_is_bounded(client, limit):
    response = client.get(f"/semantic/audit?limit={limit}", headers=headers("steward"))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "validation_error")


def test_audit_endpoint_has_no_path_parameter(client):
    response = client.get("/semantic/audit?path=/etc/passwd", headers=headers("steward"))
    assert response.status_code == 200
    assert all(e["endpoint"] != "/etc/passwd" for e in response.json()["entries"])


# --- Key-leak test (brief §7/§9) --------------------------------------------------------------------------------

LEAK_KEY = "LEAKCHECK-7f3a9c1e-distinctive-analyst-key"
LEAK_WRONG = "LEAKCHECK-deadbeef-distinctive-wrong-key"


def test_api_key_never_appears_in_logs_audit_errors_or_openapi(tmp_path, caplog, capfd):
    keys_file = tmp_path / "keys.yaml"
    keys_file.write_text(f"keys:\n  - {{label: leak-analyst, role: analyst, key: {LEAK_KEY}}}\n", encoding="utf-8")
    audit_file = tmp_path / "audit.jsonl"
    client = TestClient(create_app(keys_path=keys_file, audit_path=audit_file), raise_server_exceptions=False)
    good = {"X-API-Key": LEAK_KEY, "X-Via": LEAK_KEY}
    caplog.set_level(logging.DEBUG)

    responses = [
        client.get("/semantic/concepts", headers=good),
        client.get("/semantic/concepts/active_member", headers=good),
        client.get("/semantic/concepts/eligible_for_follow_up", headers=good),  # 403
        client.get("/semantic/concepts/no_such", headers=good),  # 404
        client.get("/semantic/concepts/active_member?as_of=bad", headers=good),  # 422
        client.post("/semantic/concepts/active_member/evaluate", json={"requested_date": "x", "facts": 1}, headers=good),
        client.post("/semantic/concepts/active_member/evaluate", json={"requested_date": "2026-01-01", "facts": {}}, headers=good),
        client.delete("/semantic/concepts", headers=good),  # 405
        client.get("/semantic/audit", headers=good),  # 403
        client.get("/semantic/concepts", headers={"X-API-Key": LEAK_WRONG}),  # 401
        client.get("/semantic/nope", headers={"X-API-Key": LEAK_WRONG}),  # 401
        client.get("/openapi.json"),
        client.get("/docs"),
    ]
    assert len(audit_file.read_text(encoding="utf-8").splitlines()) == len(responses)

    haystacks = {
        "response bodies": "\n".join(r.text for r in responses),
        "response headers": "\n".join(str(dict(r.headers)) for r in responses),
        "audit file": audit_file.read_text(encoding="utf-8"),
        "log records": "\n".join(f"{r.getMessage()} {r.args}" for r in caplog.records),
        "stdout/stderr": "".join(capfd.readouterr()),
    }
    for where, text in haystacks.items():
        for secret in (LEAK_KEY, LEAK_WRONG, "LEAKCHECK"):
            assert secret not in text, f"key material found in {where}"
