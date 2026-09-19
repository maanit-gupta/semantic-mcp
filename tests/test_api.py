"""HTTP-level tests. Endpoint coverage grows with M3/M4; M0 only has /health."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_is_public_and_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
