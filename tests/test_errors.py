from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import install_error_handlers


def test_unhandled_exception_returns_envelope():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("very secret internal message")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "very secret internal message" not in body["error"]["message"]
