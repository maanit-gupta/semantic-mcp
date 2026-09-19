"""Thin MCP wrapper over the glossary API: three tools, no business logic, no catalog access.

Each tool is one or two HTTP calls to the API with the caller's key forwarded and `X-Via: mcp`, so roles, audit and
error semantics are exactly the API's. It never imports `app`: the API is the only source of meaning. On Streamable
HTTP the key is the caller's own header; on stdio (one local user) it comes from SEMANTIC_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Annotated, Any
from urllib.parse import quote

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_API_URL = "http://127.0.0.1:8000"
TIMEOUT = httpx2.Timeout(10.0)

# Structured results: the API's JSON object, passed through unchanged. Error results carry the API's own envelope.
ToolResult = Annotated[CallToolResult, dict[str, Any]]

mcp = MCPServer(
    "ryan-mcp-glossary",
    instructions=(
        "Approved business meanings for terms such as 'member'. Before answering any question that uses a business "
        "term, call resolve_term. If it returns status 'ambiguous', ask the user its clarifying_question and wait; "
        "never pick a meaning yourself. If it returns 'restricted', tell the user they lack access. Definitions only: "
        "this server never returns data about people."
    ),
)


class _ApiFailure(Exception):
    def __init__(self, http_status: int | None, error: dict[str, Any]):
        super().__init__(error.get("code"))
        self.http_status, self.error = http_status, error


def _api_key(ctx: Context) -> str | None:
    headers = ctx.headers
    if headers is None:  # stdio: a single local user, configured by whoever launched the process
        return os.environ.get("SEMANTIC_API_KEY") or None
    # HTTP: only the caller's own key, and only if there is exactly one; never a server-side fallback, which would
    # hand every unauthenticated caller that identity. The API then decides who the caller is.
    values = headers.getlist("x-api-key") if hasattr(headers, "getlist") else [headers.get("x-api-key")]
    values = [v for v in values if v]
    return values[0] if len(values) == 1 else None


async def _api(ctx: Context, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    headers = {"X-Via": "mcp"}
    key = _api_key(ctx)
    if key is not None:
        headers["X-API-Key"] = key
    base_url = os.environ.get("SEMANTIC_API_URL", DEFAULT_API_URL).rstrip("/")
    try:
        async with httpx2.AsyncClient(base_url=base_url, timeout=TIMEOUT) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx2.HTTPError:
        # No exception text: it can include the URL and headers of the failed request.
        raise _ApiFailure(None, {"code": "api_unreachable", "message": "the glossary API could not be reached", "details": {}}) from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, dict):
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            error = {"code": "unexpected_response", "message": f"HTTP {response.status_code}", "details": {}}
        raise _ApiFailure(response.status_code, error)
    return body


def _result(payload: dict[str, Any], is_error: bool = False) -> CallToolResult:
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload, is_error=is_error)


def _error(failure: _ApiFailure) -> CallToolResult:
    return _result({"http_status": failure.http_status, "error": failure.error}, is_error=True)


def _path_id(concept_id: str) -> str:
    # Escaped so an id can never add path segments (e.g. "../audit").
    return quote(concept_id, safe="")


class ResolveContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: Annotated[
        str | None,
        Field(description="System the question is about: crm, enrollment, claims, analytics, customer_service, "
                          "care_management or network."),
    ] = None
    domain: Annotated[str | None, Field(description="Business domain, e.g. eligibility, reporting, outreach.")] = None


DATE = "Date as YYYY-MM-DD."


@mcp.tool(
    description="""Find the approved business meaning of a term (for example "member" or "active member").

When to call: FIRST, before answering any question that uses a business term, even one you think you know. The same
word means different things in different systems; this tool returns the approved meaning, or says it cannot choose.
When not to call: to get data about people (it returns definitions only), or when you already have a concept id
(use get_definition).

Arguments: `term` as the user wrote it. `context.system` when the question names or implies a system (crm,
enrollment, claims, analytics, customer_service, care_management, network). `as_of` (YYYY-MM-DD) when the question
is about a specific date; otherwise today's UTC date is used.

Read `status`:
- "resolved": `concept` is the one approved meaning. Use `concept.definition`, cite `concept.authoritative_source`
  (system and dataset of record) and `concept.rule.text` (how it is calculated); note `version`, `status`,
  `effective_from`/`effective_to`. Read `warnings`: a deprecated concept names its replacement, and a non-zero
  `restricted_count` means other meanings exist that the user cannot see; mention that.
- "ambiguous": more than one meaning fits. Do NOT choose one and do NOT guess. Ask the user `clarifying_question`,
  wait for the answer, then call again with `context.system` set to it. `candidates` lists the options (id, context,
  definition). If `warnings` says the context matched nothing, tell the user that too.
- "restricted": the meaning exists but the user's role may not see it (`restricted_count` says how many). Tell the
  user they do not have access. Do not guess the definition or try to obtain it another way.
- "not_found": there is no approved meaning. Offer `suggestions` if any. Never invent a definition.
Other fields: `term` (normalised), `as_of` (date used).
Failures come back as an error result with `error.code`, e.g. "unauthenticated" (missing or invalid API key) or
"validation_error" (e.g. a bad date); `http_status` is the API's status code.""",
)
async def resolve_term(
    term: Annotated[str, Field(description="The business term as the user wrote it, e.g. 'member'.")],
    ctx: Context,
    context: Annotated[ResolveContext | None, Field(description="Optional context used to pick one meaning.")] = None,
    as_of: Annotated[str | None, Field(description=DATE + " Defaults to today's UTC date.")] = None,
) -> ToolResult:
    body: dict[str, Any] = {"term": term}
    if context is not None:
        body["context"] = context.model_dump(exclude_none=True)
    if as_of is not None:
        body["as_of"] = as_of
    try:
        return _result(await _api(ctx, "POST", "/semantic/resolve", json=body))
    except _ApiFailure as failure:
        return _error(failure)


@mcp.tool(
    description="""Get the full approved definition of one concept by id (for example "active_member"), with its
relationships.

When to call: after resolve_term has given you a concept id, or when the user names an id. When not to call: to
search by word (use resolve_term), or to check a person against the rule (use evaluate_concept).

Arguments: `concept_id`; `as_of` (YYYY-MM-DD) selects the version effective on that date (default today's UTC date).

Result fields: `definition`; `authoritative_source` (system and dataset of record); `source_systems`; `rule.text`
(the calculation, for people) and `rule.expression` (the same rule as executed); `relationships` (outgoing:
depends_on, alternative_meaning_of, supersedes); `incoming_relationships` (concepts that point at this one; one you
may not see shows only `access: "restricted"`); `version`; `status` (approved or deprecated); `effective_from` /
`effective_to`; `owner`; `allowed_roles`; `warnings` (a deprecated concept names its replacement: prefer it).

Failures come back as an error result with `error.code`: "forbidden" (the user's role may not see this concept: tell
them, do not work around it; `error.details.required_role` lists who may), "not_found" (unknown id, or a draft),
"not_effective" (no version on that date), "validation_error", "unauthenticated".""",
)
async def get_definition(
    concept_id: Annotated[str, Field(description="Concept id, e.g. 'active_member'.")],
    ctx: Context,
    as_of: Annotated[str | None, Field(description=DATE + " Defaults to today's UTC date.")] = None,
) -> ToolResult:
    params = {"as_of": as_of} if as_of is not None else {}
    try:
        concept = await _api(ctx, "GET", f"/semantic/concepts/{_path_id(concept_id)}", params=params)
        # Same date for both calls, even if the default "today" rolls over in between.
        relationships = await _api(
            ctx, "GET", f"/semantic/concepts/{_path_id(concept_id)}/relationships", params={"as_of": concept["as_of"]}
        )
    except _ApiFailure as failure:
        return _error(failure)
    return _result({**concept, "incoming_relationships": relationships["incoming"]})


@mcp.tool(
    description="""Apply a concept's approved rule to facts the user supplies, on one date. Example: is a person with
coverage from 2026-01-01 and no end date an active_member on 2026-09-20?

When to call: only when the user gives you the facts. The service holds no data about people; never invent facts to
fill gaps. When not to call: to learn what a term means (use resolve_term or get_definition).

Arguments: `concept_id`; `requested_date` (YYYY-MM-DD); `facts`, a map of fact name to value (dates as YYYY-MM-DD;
null only where the value is genuinely empty, such as an open-ended coverage_end_date). Leaving a fact out is not the
same as null: a left-out fact is reported as missing.

Result fields: `result` (true or false), `rule_text` (the rule applied), `source` (system and dataset of record),
`version` (the version effective on requested_date), `status`, `requested_date`, `warnings` (e.g. deprecated).

Failures come back as an error result with `error.code`: "insufficient_context" (`error.details.missing_facts` lists
the facts to ask the user for; never assume them), "invalid_facts" (`error.details.errors` per fact),
"forbidden", "not_found", "not_effective", "not_evaluable", "validation_error", "unauthenticated".""",
)
async def evaluate_concept(
    concept_id: Annotated[str, Field(description="Concept id, e.g. 'active_member'.")],
    requested_date: Annotated[str, Field(description=DATE + " The date the rule is evaluated for.")],
    facts: Annotated[dict[str, Any], Field(description="Fact name to value, e.g. {\"coverage_start_date\": \"2026-01-01\", \"coverage_end_date\": null}.")],
    ctx: Context,
) -> ToolResult:
    body = {"requested_date": requested_date, "facts": facts}
    try:
        return _result(await _api(ctx, "POST", f"/semantic/concepts/{_path_id(concept_id)}/evaluate", json=body))
    except _ApiFailure as failure:
        return _error(failure)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MCP server for the Ryan-MCP glossary API.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
