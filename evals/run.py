"""Run the ambiguous-question eval: `python -m evals.run`. Exits 0 only if every check passes.

Every case goes through the pure resolver (the unit under test). Cases marked `mcp: true` also go through the real
MCP tool path: the API runs in-process on a free local port with throwaway random keys, and resolve_term is called
through an MCP client, so the answer crosses the MCP server and HTTP exactly as an agent's would.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import uvicorn
import yaml

from app.auth import Caller
from app.catalog import load_catalog
from app.main import DEFAULT_CATALOG_PATH, create_app
from app.resolver import resolve

CASES_PATH = Path(__file__).with_name("ambiguous_questions.yaml")
ROLES = ("analyst", "care_manager", "steward")


def observed_from_resolution(result) -> dict[str, Any]:
    concepts = [result.concept.id] if result.concept else [c.id for c in result.candidates]
    return {
        "status": result.status, "concepts": concepts, "version": result.concept.version if result.concept else None,
        "suggestions": list(result.suggestions), "restricted_count": result.restricted_count,
        "warnings": list(result.warnings),
    }


def observed_from_body(body: dict[str, Any]) -> dict[str, Any]:
    concept = body.get("concept")
    concepts = [concept["id"]] if concept else [c["id"] for c in body.get("candidates", [])]
    return {
        "status": body.get("status"), "concepts": concepts, "version": concept["version"] if concept else None,
        "suggestions": body.get("suggestions", []), "restricted_count": body.get("restricted_count"),
        "warnings": body.get("warnings", []),
    }


def failures(case: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    problems = []
    if observed["status"] != case["expected_status"]:
        problems.append(f"status {observed['status']!r} != {case['expected_status']!r}")
    if observed["concepts"] != case["expected_concepts"]:
        problems.append(f"concepts {observed['concepts']} != {case['expected_concepts']}")
    if "expected_version" in case and observed["version"] != case["expected_version"]:
        problems.append(f"version {observed['version']!r} != {case['expected_version']!r}")
    if "expected_suggestions" in case and observed["suggestions"] != case["expected_suggestions"]:
        problems.append(f"suggestions {observed['suggestions']} != {case['expected_suggestions']}")
    if "expected_restricted_count" in case and observed["restricted_count"] != case["expected_restricted_count"]:
        problems.append(f"restricted_count {observed['restricted_count']} != {case['expected_restricted_count']}")
    if "expected_warning" in case and not any(case["expected_warning"] in w for w in observed["warnings"]):
        problems.append(f"no warning containing {case['expected_warning']!r}")
    return problems


def _context(case: dict[str, Any]) -> dict[str, str]:
    return case.get("context") or {}


def _as_of(case: dict[str, Any]) -> date:
    value = case["as_of"]
    return value if isinstance(value, date) else date.fromisoformat(value)


def run_resolver(cases: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    results = []
    for case in cases:
        ctx = _context(case)
        result = resolve(catalog, case["term"], _as_of(case), Caller(f"eval-{case['role']}", case["role"]),
                         ctx.get("system"), ctx.get("domain"))
        results.append((case, observed_from_resolution(result)))
    return results


@contextmanager
def _api_on_free_port() -> Iterator[tuple[str, dict[str, str]]]:
    """The real API app on 127.0.0.1:<free port>, with random keys and an audit file that disappear afterwards."""
    keys = {role: secrets.token_urlsafe(24) for role in ROLES}
    with tempfile.TemporaryDirectory(prefix="semantic-eval-") as tmp:
        keys_file = Path(tmp) / "keys.yaml"
        keys_file.write_text(yaml.safe_dump({"keys": [{"label": f"eval-{r}", "role": r, "key": k} for r, k in keys.items()]}),
                             encoding="utf-8")
        app = create_app(keys_path=keys_file, audit_path=Path(tmp) / "audit.jsonl")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the API did not start for the MCP eval path")
            time.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}", keys
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def run_mcp(cases: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from mcp import Client

    from mcp_server.server import mcp as mcp_server

    async def call(case: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {"term": case["term"], "as_of": _as_of(case).isoformat()}
        if case.get("context"):
            args["context"] = case["context"]
        async with Client(mcp_server) as client:
            result = await client.call_tool("resolve_term", args)
        if result.is_error:
            return {"status": f"tool error: {result.structured_content}", "concepts": [], "version": None,
                    "suggestions": [], "restricted_count": None, "warnings": []}
        return observed_from_body(result.structured_content)

    saved = {name: os.environ.get(name) for name in ("SEMANTIC_API_URL", "SEMANTIC_API_KEY")}
    results = []
    try:
        with _api_on_free_port() as (url, keys):
            os.environ["SEMANTIC_API_URL"] = url
            for case in cases:
                # In-process MCP calls take the stdio path: the key comes from SEMANTIC_API_KEY.
                os.environ["SEMANTIC_API_KEY"] = keys[case["role"]]
                results.append((case, asyncio.run(call(case))))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ambiguous-question eval.")
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="YAML file of cases (default: %(default)s)")
    args = parser.parse_args(argv)
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one INFO line per API request would bury the table
    cases = yaml.safe_load(args.cases.read_text(encoding="utf-8"))["cases"]

    rows = [("resolver", case, observed) for case, observed in run_resolver(cases)]
    rows += [("mcp", case, observed) for case, observed in run_mcp([c for c in cases if c.get("mcp")])]

    failed = 0
    print(f"{'case':32} {'path':8} {'expected':12} {'observed':12} {'concepts':46} result")
    print("-" * 120)
    for path, case, observed in rows:
        problems = failures(case, observed)
        failed += bool(problems)
        concepts = ",".join(observed["concepts"]) or "-"
        if observed["version"]:
            concepts += f" v{observed['version']}"
        print(f"{case['id']:32} {path:8} {case['expected_status']:12} {str(observed['status'])[:12]:12} "
              f"{concepts[:46]:46} {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}")
    print("-" * 120)
    print(f"{len(rows) - failed}/{len(rows)} checks passed ({len(cases)} cases; {sum(1 for c in cases if c.get('mcp'))} also via MCP)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
