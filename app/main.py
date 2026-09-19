"""FastAPI entry point.

Kept deliberately thin: routes delegate to the catalog, rules and (later) resolver modules so that business
logic stays testable without HTTP.
"""
from fastapi import FastAPI

app = FastAPI(title="Ryan-MCP semantic glossary", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    # Public liveness probe: returns no catalog content, so it needs no API key.
    return {"status": "ok"}
