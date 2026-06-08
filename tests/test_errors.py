from fastapi import FastAPI, HTTPException
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


def test_http_exception_headers_are_preserved():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/limited")
    def limited():
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "60"},
            detail={"code": "rate_limited", "message": "Too many requests"},
        )

    client = TestClient(app)
    response = client.get("/limited")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["error"]["code"] == "rate_limited"
