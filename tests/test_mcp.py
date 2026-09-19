"""MCP server tests against a real API process: tool listing and schemas, structured results and structured tool
errors, key forwarding (stdio: environment; HTTP: the caller's own header, never a fallback), X-Via on every call,
and the real protocol over a stdio subprocess and Streamable HTTP.
"""
import ast
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from mcp_server import server as mcp_module
from tests.conftest import FIXTURES, KEYS

ROOT = Path(__file__).resolve().parent.parent
RESTRICTED_TEXT = ["Active member eligible for outreach", "care_gap_follow_up", "outreach eligible"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, proc: subprocess.Popen, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited early: {proc.stderr.read().decode(errors='replace')[-2000:]}")
        try:
            httpx2.get(url, timeout=0.5)
            return
        except httpx2.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        _, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err.decode(errors="replace")


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """The real API on a free port, with the test keys and its own audit file."""
    port, audit = free_port(), tmp_path_factory.mktemp("api") / "audit.jsonl"
    env = {**os.environ, "SEMANTIC_API_KEYS_FILE": str(FIXTURES / "api_keys.yaml"), "SEMANTIC_AUDIT_LOG": str(audit)}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    wait_for(url + "/health", proc)
    yield {"url": url, "audit": audit}
    stop(proc)


@pytest.fixture(scope="module")
def mcp_http(api):
    """The MCP server over Streamable HTTP. It is given a *steward* key in its environment on purpose: HTTP callers
    must never inherit it."""
    port = free_port()
    env = {**os.environ, "SEMANTIC_API_URL": api["url"], "SEMANTIC_API_KEY": KEYS["steward"]}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server.server", "--transport", "http", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_for(f"http://127.0.0.1:{port}/mcp", proc)
    info = {"url": f"http://127.0.0.1:{port}/mcp", "stderr": ""}
    yield info
    info["stderr"] = stop(proc)


def audit_lines(api) -> list[dict]:
    return [json.loads(line) for line in api["audit"].read_text(encoding="utf-8").splitlines()]


def run(coro):
    return asyncio.run(coro)


async def call_inproc(tool: str, args: dict):
    async with Client(mcp_module.mcp) as client:
        return await client.call_tool(tool, args)


async def call_http(url: str, headers, tool: str, args: dict):
    async with httpx2.AsyncClient(headers=headers) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            return await client.call_tool(tool, args)


def stdio_params(api, key: str | None) -> StdioServerParameters:
    env = {"SEMANTIC_API_URL": api["url"]}
    if key is not None:
        env["SEMANTIC_API_KEY"] = key
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT), env=env)


@pytest.fixture
def as_role(api, monkeypatch):
    """In-process calls behave like stdio (no request headers), so the key comes from SEMANTIC_API_KEY."""
    monkeypatch.setenv("SEMANTIC_API_URL", api["url"])

    def use(role: str | None):
        if role is None:
            monkeypatch.delenv("SEMANTIC_API_KEY", raising=False)
        else:
            monkeypatch.setenv("SEMANTIC_API_KEY", KEYS.get(role, role))

    return use


# --- Tool listing -----------------------------------------------------------------------------------------------


def test_exactly_three_tools_with_agent_guidance():
    async def listed():
        async with Client(mcp_module.mcp) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in run(listed())}
    assert sorted(tools) == ["evaluate_concept", "get_definition", "resolve_term"]
    resolve = tools["resolve_term"].description
    for phrase in ("FIRST, before answering", '"ambiguous"', "Do NOT choose one and do NOT guess",
                   "clarifying_question", '"restricted"', "Never invent a definition"):
        assert phrase in resolve
    assert "never invent facts" in tools["evaluate_concept"].description
    assert "missing_facts" in tools["evaluate_concept"].description
    assert "do not work around it" in tools["get_definition"].description
    assert [tools[n].input_schema["required"] for n in ("resolve_term", "get_definition", "evaluate_concept")] == [
        ["term"], ["concept_id"], ["concept_id", "requested_date", "facts"]
    ]
    for tool in tools.values():
        assert "ctx" not in tool.input_schema["properties"]
        assert tool.output_schema == {"additionalProperties": True, "title": f"{tool.name}DictOutput", "type": "object"}


# --- Structured results -----------------------------------------------------------------------------------------


def test_resolve_member_is_ambiguous_with_five_candidates(as_role, api):
    as_role("analyst")
    result = run(call_inproc("resolve_term", {"term": "member", "as_of": "2026-09-20"}))
    body = result.structured_content
    assert (result.is_error, body["status"], body["restricted_count"]) == (False, "ambiguous", 0)
    assert [c["id"] for c in body["candidates"]] == [
        "reporting_month_member", "claimant_member", "registered_member", "currently_eligible_member", "active_member"
    ]
    # Passed through unchanged: identical to what the API returns directly.
    direct = httpx2.post(api["url"] + "/semantic/resolve", json={"term": "member", "as_of": "2026-09-20"},
                         headers={"X-API-Key": KEYS["analyst"]}).json()
    assert body == direct
    assert json.loads(result.content[0].text) == body


def test_resolve_with_context_is_resolved(as_role):
    as_role("analyst")
    body = run(call_inproc("resolve_term", {"term": "member", "context": {"system": "enrollment"}})).structured_content
    assert (body["status"], body["concept"]["id"], body["concept"]["authoritative_source"]["system"]) == (
        "resolved", "active_member", "eligibility_enrollment_platform"
    )


def test_get_definition_adds_incoming_relationships(as_role):
    as_role("care_manager")
    body = run(call_inproc("get_definition", {"concept_id": "active_member", "as_of": "2026-09-20"})).structured_content
    assert (body["id"], body["version"], body["as_of"], body["rule"]["text"][:30]) == (
        "active_member", "1.0.0", "2026-09-20", "coverage_start_date <= request"
    )
    assert [r["source"] for r in body["incoming_relationships"]] == ["eligible_for_follow_up", "member_in_network"]


def test_evaluate_concept_result(as_role):
    as_role("analyst")
    facts = {"coverage_start_date": "2026-01-01", "coverage_end_date": None, "network_status": "in_network"}
    body = run(call_inproc("evaluate_concept", {"concept_id": "member_in_network", "requested_date": "2026-09-20", "facts": facts})).structured_content
    assert (body["result"], body["version"], body["rule_text"]) == (
        True, "1.0.0", "network_status == 'in_network' AND active_member == TRUE"
    )


# --- Structured tool errors -------------------------------------------------------------------------------------


def test_restricted_definition_is_a_structured_error_without_content(as_role):
    as_role("analyst")
    result = run(call_inproc("get_definition", {"concept_id": "eligible_for_follow_up"}))
    assert result.is_error is True
    assert result.structured_content == {
        "http_status": 403,
        "error": {"code": "forbidden", "message": "your role cannot access 'eligible_for_follow_up'",
                  "details": {"concept_id": "eligible_for_follow_up", "required_role": ["care_manager", "steward"]}},
    }
    text = result.content[0].text
    assert json.loads(text) == result.structured_content
    for secret in RESTRICTED_TEXT:
        assert secret not in text


@pytest.mark.parametrize(
    ("tool", "args", "status", "code"),
    [
        ("evaluate_concept", {"concept_id": "member_in_network", "requested_date": "2026-09-20", "facts": {"network_status": "x"}}, 422, "insufficient_context"),
        ("evaluate_concept", {"concept_id": "active_member", "requested_date": "2026-02-30", "facts": {}}, 422, "validation_error"),
        ("get_definition", {"concept_id": "engaged_member"}, 404, "not_found"),
        ("get_definition", {"concept_id": "active_member", "as_of": "2020-01-01"}, 404, "not_effective"),
        ("resolve_term", {"term": "   "}, 422, "validation_error"),
    ],
)
def test_api_errors_become_structured_tool_errors(as_role, tool, args, status, code):
    as_role("analyst")
    result = run(call_inproc(tool, args))
    assert (result.is_error, result.structured_content["http_status"], result.structured_content["error"]["code"]) == (
        True, status, code
    )


def test_missing_facts_reach_the_agent(as_role):
    as_role("analyst")
    args = {"concept_id": "member_in_network", "requested_date": "2026-09-20", "facts": {"network_status": "out_of_network"}}
    error = run(call_inproc("evaluate_concept", args)).structured_content["error"]
    assert error["details"] == {"missing_facts": ["coverage_end_date", "coverage_start_date"]}


def test_unreachable_api_is_a_structured_error(monkeypatch):
    monkeypatch.setenv("SEMANTIC_API_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("SEMANTIC_API_KEY", KEYS["analyst"])
    result = run(call_inproc("resolve_term", {"term": "member"}))
    assert (result.is_error, result.structured_content) == (
        True, {"http_status": None, "error": {"code": "api_unreachable", "message": "the glossary API could not be reached", "details": {}}}
    )
    assert "Traceback" not in result.content[0].text and KEYS["analyst"] not in result.content[0].text


def test_concept_id_cannot_add_path_segments(as_role, api):
    as_role("steward")
    result = run(call_inproc("get_definition", {"concept_id": "../audit"}))
    # Escaped as ..%2Faudit: the server decodes it to one path that matches no route, so the steward's key never
    # reaches the audit endpoint.
    assert (result.is_error, result.structured_content["http_status"], result.structured_content["error"]["message"]) == (
        True, 404, "no such route"
    )
    last = audit_lines(api)[-1]
    assert (last["endpoint"], last["status_code"]) == ("/semantic/concepts/../audit", 404)


# --- Key forwarding (stdio / in-process: environment) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [(None, (True, "unauthenticated")), ("wrong-key-0000000000", (True, "unauthenticated")),
     ("analyst", (False, "restricted")), ("care_manager", (False, "resolved"))],
)
def test_env_key_is_forwarded(as_role, role, expected):
    as_role(role)
    result = run(call_inproc("resolve_term", {"term": "eligible for follow up"}))
    body = result.structured_content
    assert (result.is_error, body.get("status") or body["error"]["code"]) == expected


def test_every_api_call_says_via_mcp(as_role, api):
    as_role("analyst")
    before = len(audit_lines(api))
    run(call_inproc("get_definition", {"concept_id": "active_member"}))
    new = audit_lines(api)[before:]
    assert [(e["endpoint"], e["via"], e["role"]) for e in new] == [
        ("/semantic/concepts/{concept_id}", "mcp", "analyst"),
        ("/semantic/concepts/{concept_id}/relationships", "mcp", "analyst"),
    ]


# --- Real protocol: stdio subprocess ----------------------------------------------------------------------------


def test_stdio_subprocess_end_to_end(api):
    async def session(key):
        async with Client(stdio_params(api, key)) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            member = await client.call_tool("resolve_term", {"term": "member"})
            restricted = await client.call_tool("get_definition", {"concept_id": "eligible_for_follow_up"})
            return names, member, restricted

    before = len(audit_lines(api))
    names, member, restricted = run(session(KEYS["analyst"]))
    assert names == ["evaluate_concept", "get_definition", "resolve_term"]
    assert (member.structured_content["status"], len(member.structured_content["candidates"])) == ("ambiguous", 5)
    assert (restricted.is_error, restricted.structured_content["error"]["code"]) == (True, "forbidden")
    assert [(e["via"], e["role"], e["outcome"]) for e in audit_lines(api)[before:]] == [
        ("mcp", "analyst", "ambiguous"), ("mcp", "analyst", "denied")
    ]


def test_stdio_subprocess_without_key_is_unauthenticated(api):
    async def session():
        async with Client(stdio_params(api, None)) as client:
            return await client.call_tool("resolve_term", {"term": "member"})

    result = run(session())
    assert (result.is_error, result.structured_content["http_status"], result.structured_content["error"]["code"]) == (
        True, 401, "unauthenticated"
    )


# --- Real protocol: Streamable HTTP -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, (True, "unauthenticated")),
        ({"X-API-Key": "wrong-key-0000000000"}, (True, "unauthenticated")),
        ({"X-API-Key": KEYS["analyst"]}, (False, "restricted")),
        ({"X-API-Key": KEYS["care_manager"]}, (False, "resolved")),
        ([("X-API-Key", KEYS["analyst"]), ("X-API-Key", KEYS["care_manager"])], (True, "unauthenticated")),
    ],
    ids=["no-key", "wrong-key", "analyst", "care-manager", "two-keys"],
)
def test_http_forwards_only_the_callers_own_key(mcp_http, headers, expected):
    # The server process holds a steward key in its environment; an HTTP caller without a key must still get 401.
    result = run(call_http(mcp_http["url"], headers, "resolve_term", {"term": "eligible for follow up"}))
    body = result.structured_content
    assert (result.is_error, body.get("status") or body["error"]["code"]) == expected


def test_http_member_ambiguous_and_audited_via_mcp(mcp_http, api):
    before = len(audit_lines(api))
    result = run(call_http(mcp_http["url"], {"X-API-Key": KEYS["analyst"]}, "resolve_term", {"term": "member"}))
    assert (result.structured_content["status"], len(result.structured_content["candidates"])) == ("ambiguous", 5)
    (entry,) = audit_lines(api)[before:]
    assert (entry["via"], entry["key_label"], entry["outcome"]) == ("mcp", "test-analyst", "ambiguous")


# --- Hygiene ----------------------------------------------------------------------------------------------------


def test_mcp_server_does_not_import_the_app():
    tree = ast.parse((ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any(m == "app" or m.startswith("app.") for m in modules if m)


def test_keys_never_appear_in_mcp_server_output_or_results(api, as_role):
    # A dedicated HTTP server so its whole stderr (SDK and httpx2 logs at their default INFO level) can be read after
    # it stops.
    port = free_port()
    env = {**os.environ, "SEMANTIC_API_URL": api["url"], "SEMANTIC_API_KEY": KEYS["steward"]}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server.server", "--transport", "http", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}/mcp"
    wait_for(url, proc)
    results = [
        run(call_http(url, {"X-API-Key": KEYS["analyst"]}, "get_definition", {"concept_id": "eligible_for_follow_up"})),
        run(call_http(url, {"X-API-Key": KEYS["care_manager"]}, "resolve_term", {"term": "member"})),
        run(call_http(url, {"X-API-Key": "wrong-key-0000000000"}, "resolve_term", {"term": "member"})),
    ]
    stderr = stop(proc)
    as_role("analyst")
    results.append(run(call_inproc("resolve_term", {"term": "member"})))
    assert "HTTP Request" in stderr  # the log was captured, so the scan below is meaningful
    haystacks = [stderr, api["audit"].read_text(encoding="utf-8")] + [r.content[0].text for r in results]
    for key in [*KEYS.values(), "wrong-key-0000000000"]:
        assert all(key not in h for h in haystacks)
