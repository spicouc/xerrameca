from pathlib import Path

from fastapi.testclient import TestClient

from xerrameca.app import create_app


def test_health_is_standalone(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "xerrameca.db"))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "xerrameca"
