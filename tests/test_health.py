from fastapi.testclient import TestClient

from xerrameca.app import app


def test_health_is_standalone() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "xerrameca"
