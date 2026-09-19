"""Authentication and authorization: the 401/403 matrix by role x endpoint, restricted list entries, role escalation
attempts, and startup failure on bad key configuration.
"""
from pathlib import Path

import pytest

from app.auth import Caller, KeyConfigError, KeyStore, is_visible, load_keys
from app.catalog import load_catalog
from app.main import DEFAULT_CATALOG_PATH, create_app
from tests.conftest import KEYS, headers

UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "missing or invalid X-API-Key header", "details": {}}}
ACTIVE_FACTS = {"coverage_start_date": "2026-01-01", "coverage_end_date": None}
FOLLOW_UP_FACTS = {**ACTIVE_FACTS, "follow_up_eligible": True, "risk_flag": "low"}

# (method, path, json body) for every protected route, with the seed's one restricted concept where it matters.
ENDPOINTS = {
    "list": ("GET", "/semantic/concepts", None),
    "get_open": ("GET", "/semantic/concepts/active_member", None),
    "get_restricted": ("GET", "/semantic/concepts/eligible_for_follow_up", None),
    "relationships_open": ("GET", "/semantic/concepts/active_member/relationships", None),
    "relationships_restricted": ("GET", "/semantic/concepts/eligible_for_follow_up/relationships", None),
    "evaluate_open": ("POST", "/semantic/concepts/active_member/evaluate", {"requested_date": "2026-09-19", "facts": ACTIVE_FACTS}),
    "evaluate_restricted": ("POST", "/semantic/concepts/eligible_for_follow_up/evaluate", {"requested_date": "2026-09-19", "facts": FOLLOW_UP_FACTS}),
    "audit": ("GET", "/semantic/audit", None),
}

# Expected status per role. Anything not listed for a role is 200.
FORBIDDEN = {
    "analyst": {"get_restricted", "relationships_restricted", "evaluate_restricted", "audit"},
    "care_manager": {"audit"},
    "steward": set(),
}


def call(client, endpoint: str, hdrs: dict | None = None):
    method, path, body = ENDPOINTS[endpoint]
    return client.request(method, path, json=body, headers=hdrs or {})


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize("role", ["analyst", "care_manager", "steward"])
def test_role_by_endpoint_matrix(client, endpoint, role):
    response = call(client, endpoint, headers(role))
    if endpoint in FORBIDDEN[role]:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
    else:
        assert response.status_code == 200


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize(
    "hdrs",
    [
        {},
        {"X-API-Key": ""},
        {"X-API-Key": "wrong-key-0000000000"},
        {"X-API-Key": KEYS["analyst"] + " "},  # no trimming: a near-miss is a miss
        {"X-API-Key": KEYS["analyst"].upper()},
        {"Authorization": f"Bearer {KEYS['steward']}"},  # only X-API-Key is read
    ],
    ids=["missing", "empty", "wrong", "trailing-space", "wrong-case", "other-header"],
)
def test_missing_or_invalid_key_is_401_everywhere(client, endpoint, hdrs):
    response = call(client, endpoint, hdrs)
    assert (response.status_code, response.json()) == (401, UNAUTHENTICATED)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/semantic/no-such-route", {}),
        ("GET", "/anything/else", {}),
        ("POST", "/semantic/concepts/active_member/evaluate", {"content": b"{not json", "headers": {"Content-Type": "application/json"}}),
        ("POST", "/semantic/concepts/active_member/evaluate", {"json": {"requested_date": "bad"}}),
        ("DELETE", "/semantic/concepts", {}),
        ("GET", "/semantic/concepts/active_member?as_of=not-a-date", {}),
    ],
    ids=["unknown-semantic-path", "unknown-path", "malformed-json", "invalid-body", "wrong-method", "bad-query"],
)
def test_unauthenticated_gets_the_same_401_before_routing_or_validation(client, method, path, kwargs):
    response = client.request(method, path, **kwargs)
    assert (response.status_code, response.json()) == (401, UNAUTHENTICATED)


@pytest.mark.parametrize("raw", ["clé-non-ascii-key-000".encode("utf-8"), b"\xff\xfe latin-1 bytes 0000000"])
def test_non_ascii_key_header_is_401_not_500(client, raw):
    response = client.get("/semantic/concepts", headers={"X-API-Key": raw})
    assert (response.status_code, response.json()) == (401, UNAUTHENTICATED)


@pytest.mark.parametrize(
    "values",
    [[KEYS["analyst"], KEYS["steward"]], [KEYS["steward"], KEYS["steward"]], ["wrong-key-0000000000", KEYS["steward"]]],
    ids=["two-valid-keys", "same-key-twice", "wrong-then-valid"],
)
def test_several_key_headers_are_401(client, values):
    response = client.get("/semantic/audit", headers=[("X-API-Key", v) for v in values])
    assert (response.status_code, response.json()) == (401, UNAUTHENTICATED)


def test_header_name_is_case_insensitive(client):
    assert client.get("/semantic/concepts", headers={"x-api-key": KEYS["analyst"]}).status_code == 200


@pytest.mark.parametrize("path", ["/health", "/openapi.json", "/docs"])
def test_public_paths_need_no_key(client, path):
    assert client.get(path).status_code == 200


# --- Role escalation attempts -----------------------------------------------------------------------------------


def test_role_in_body_is_rejected_not_honoured(client):
    body = {"requested_date": "2026-09-19", "facts": FOLLOW_UP_FACTS, "role": "steward"}
    response = client.post("/semantic/concepts/eligible_for_follow_up/evaluate", json=body, headers=headers("analyst"))
    assert response.status_code == 422
    assert response.json()["error"]["details"]["errors"] == [{"loc": "body.role", "message": "Extra inputs are not permitted"}]


@pytest.mark.parametrize("query", ["role=steward", "role=care_manager&key_label=test-steward", "api_key=test-steward-key-0003"])
def test_role_in_query_is_ignored(client, query):
    assert client.get(f"/semantic/audit?{query}", headers=headers("analyst")).status_code == 403
    assert client.get(f"/semantic/concepts/eligible_for_follow_up?{query}", headers=headers("analyst")).status_code == 403


def test_x_via_header_does_not_change_authorization(client):
    response = client.get("/semantic/audit", headers=headers("analyst", **{"X-Via": "mcp"}))
    assert response.status_code == 403


# --- Restricted content -----------------------------------------------------------------------------------------


def test_forbidden_body_names_required_roles_and_nothing_else(client):
    response = client.get("/semantic/concepts/eligible_for_follow_up", headers=headers("analyst"))
    assert response.json() == {
        "error": {
            "code": "forbidden",
            "message": "your role cannot access 'eligible_for_follow_up'",
            "details": {"concept_id": "eligible_for_follow_up", "required_role": ["care_manager", "steward"]},
        }
    }


def test_restricted_list_entry_exposes_no_definition(client):
    entries = client.get("/semantic/concepts", headers=headers("analyst")).json()["concepts"]
    restricted = [e for e in entries if e["id"] == "eligible_for_follow_up"]
    assert restricted == [
        {"id": "eligible_for_follow_up", "name": "Eligible for follow up", "term": "eligible for follow up", "access": "restricted"}
    ]


def test_restricted_concept_is_listed_in_full_for_allowed_role(client):
    entries = client.get("/semantic/concepts", headers=headers("care_manager")).json()["concepts"]
    (entry,) = [e for e in entries if e["id"] == "eligible_for_follow_up"]
    assert (entry["access"], entry["definition"]) == (
        "full",
        "Active member eligible for outreach based on plan status and care gap follow-up rules",
    )


def test_restricted_evaluate_is_403_before_facts_are_examined(client):
    # With no facts at all an allowed role gets 422 insufficient_context; an analyst must not learn that.
    body = {"requested_date": "2026-09-19", "facts": {}}
    analyst = client.post("/semantic/concepts/eligible_for_follow_up/evaluate", json=body, headers=headers("analyst"))
    manager = client.post("/semantic/concepts/eligible_for_follow_up/evaluate", json=body, headers=headers("care_manager"))
    assert (analyst.status_code, analyst.json()["error"]["code"]) == (403, "forbidden")
    assert (manager.status_code, manager.json()["error"]["code"]) == (422, "insufficient_context")


def test_is_visible_uses_allowed_roles():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    (restricted,) = catalog.versions("eligible_for_follow_up")
    assert [is_visible(Caller("x", role), restricted) for role in ("analyst", "care_manager", "steward")] == [False, True, True]


# --- KeyStore and key file validation ---------------------------------------------------------------------------


def test_keystore_authenticates_each_role_and_rejects_others():
    store = KeyStore([("a", "analyst", "key-a-000000000000"), ("s", "steward", "key-s-000000000000")])
    assert store.authenticate("key-a-000000000000") == Caller("a", "analyst")
    assert store.authenticate("key-s-000000000000") == Caller("s", "steward")
    assert [store.authenticate(k) for k in (None, "", "key-a-00000000000", "key-a-0000000000000")] == [None] * 4


def test_no_wrong_key_authenticates():
    # Many deterministic wrong keys plus one-character near-misses of each real key: a weakened comparison (for
    # example on a prefix of the digest) would accept some of them.
    store = KeyStore([(role, role, key) for role, key in KEYS.items()])
    near_misses = [key[:i] + chr(ord(key[i]) ^ 1) + key[i + 1:] for key in KEYS.values() for i in range(len(key))]
    wrong = [f"wrong-key-{i:06d}-padding" for i in range(5000)] + near_misses + [k[:-1] for k in KEYS.values()]
    assert [k for k in wrong if store.authenticate(k) is not None] == []
    assert [store.authenticate(k).role for k in KEYS.values()] == list(KEYS)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "keys.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def key_issues(path: Path) -> list[str]:
    with pytest.raises(KeyConfigError) as excinfo:
        load_keys(path)
    return excinfo.value.issues


def test_missing_key_file_fails_clearly(tmp_path):
    assert key_issues(tmp_path / "absent.yaml") == [
        "cannot read the file (No such file or directory); copy config/api_keys.example.yaml to "
        "config/api_keys.yaml or set SEMANTIC_API_KEYS_FILE"
    ]


def test_invalid_yaml_reports_line_but_not_the_key(tmp_path):
    path = write(tmp_path, "keys:\n  - {label: a, role: analyst, key: SECRET-VALUE-123456789\n")
    with pytest.raises(KeyConfigError) as excinfo:
        load_keys(path)
    assert excinfo.value.issues == ["invalid YAML at line 3"]
    assert "SECRET-VALUE" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ["(root): Input should be a valid dictionary or instance of _KeyFile"]),
        ("keys: []\n", ["keys: List should have at least 1 item after validation, not 0"]),
        ("keys:\n  - {label: a, role: admin, key: SECRET-VALUE-123456789}\n", ["keys.0.role: Input should be 'analyst', 'care_manager' or 'steward'"]),
        ("keys:\n  - {label: a, role: analyst, key: SECRET-short}\n", ["keys.0.key: String should have at least 16 characters"]),
        ("keys:\n  - {label: a, role: analyst, key: 1234567890123456789}\n", ["keys.0.key: Input should be a valid string"]),
        ("keys:\n  - {label: 'Bad Label', role: analyst, key: SECRET-VALUE-123456789}\n", ["keys.0.label: String should match pattern '^[a-z0-9][a-z0-9_-]{0,63}$'"]),
        ("keys:\n  - {label: a, role: analyst, key: SECRET-VALUE-123456789, note: SECRET-x}\n", ["keys.0.note: Extra inputs are not permitted"]),
        (
            "keys:\n  - {label: a, role: analyst, key: SECRET-VALUE-123456789}\n  - {label: a, role: steward, key: SECRET-VALUE-987654321}\n",
            ["keys.1.label: duplicate label 'a'"],
        ),
        (
            "keys:\n  - {label: a, role: analyst, key: SECRET-VALUE-123456789}\n  - {label: b, role: steward, key: SECRET-VALUE-123456789}\n",
            ["keys.1.key: same key as 'a'"],
        ),
    ],
    ids=["empty-file", "no-keys", "unknown-role", "short-key", "non-string-key", "bad-label", "extra-field", "dup-label", "dup-key"],
)
def test_bad_key_file_fails_with_exact_issue_and_no_key_value(tmp_path, text, expected):
    path = write(tmp_path, text)
    with pytest.raises(KeyConfigError) as excinfo:
        load_keys(path)
    assert excinfo.value.issues == expected
    assert "SECRET" not in str(excinfo.value)


def test_all_key_file_problems_are_reported_together(tmp_path):
    path = write(tmp_path, "keys:\n  - {label: a, role: admin, key: short}\n")
    assert key_issues(path) == [
        "keys.0.role: Input should be 'analyst', 'care_manager' or 'steward'",
        "keys.0.key: String should have at least 16 characters",
    ]


def test_app_refuses_to_start_with_bad_key_file(tmp_path):
    with pytest.raises(KeyConfigError):
        create_app(keys_path=write(tmp_path, "keys: []\n"), audit_path=tmp_path / "a.jsonl")


def test_example_key_file_is_valid_and_obviously_fake():
    store_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.example.yaml"
    load_keys(store_path)
    assert all(line.split("key: ")[1].startswith("FAKE-") for line in store_path.read_text().splitlines() if "key: " in line)
