"""FastAPI entry point.

Kept deliberately thin: routes delegate to the catalog, rules and (later) resolver modules so that business
logic stays testable without HTTP. The catalog is loaded and validated when the app is built, so an invalid
catalog stops the process before it can serve anything.
"""
from pathlib import Path

from fastapi import FastAPI

from .catalog import load_catalog

# Resolved from this file, not the working directory, so `uvicorn app.main:app` works from anywhere.
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog" / "concepts.yaml"


def create_app(catalog_path: Path = DEFAULT_CATALOG_PATH) -> FastAPI:
    catalog = load_catalog(catalog_path)  # raises CatalogError listing every issue
    application = FastAPI(title="Ryan-MCP semantic glossary", version="0.1.0")
    application.state.catalog = catalog

    @application.get("/health")
    def health() -> dict[str, str]:
        # Public liveness probe: returns no catalog content, so it needs no API key.
        return {"status": "ok"}

    return application


app = create_app()
