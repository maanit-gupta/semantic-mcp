"""Test configuration: point the module-level app at fake keys and a throwaway audit file before it is imported.

Tests that inspect audit lines build their own app with `make_client`, each with its own audit file.
"""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"
os.environ["SEMANTIC_API_KEYS_FILE"] = str(FIXTURES / "api_keys.yaml")
os.environ["SEMANTIC_AUDIT_LOG"] = str(Path(tempfile.mkdtemp(prefix="semantic-audit-")) / "audit.jsonl")

KEYS = {
    "analyst": "test-analyst-key-0001",
    "care_manager": "test-care-manager-key-0002",
    "steward": "test-steward-key-0003",
}


def headers(role: str, **extra: str) -> dict[str, str]:
    return {"X-API-Key": KEYS[role], **extra}


@pytest.fixture
def audit_path(tmp_path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def client(audit_path) -> TestClient:
    from app.main import create_app

    return TestClient(create_app(audit_path=audit_path), raise_server_exceptions=False)
