from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.settings import allowed_origins


def _client():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return TestClient(app)


def test_allowed_origins_default_to_public_frontends(monkeypatch):
    monkeypatch.delenv("COLORCHASE_ALLOWED_ORIGINS", raising=False)

    assert allowed_origins() == [
        "https://colorchase.meiyoutou.top",
        "https://meiyoutou.github.io",
    ]


def test_allowed_origins_append_env_entries_without_wildcards(monkeypatch):
    monkeypatch.setenv(
        "COLORCHASE_ALLOWED_ORIGINS",
        "http://localhost:5173/, *, https://example.com/some/path",
    )

    assert allowed_origins() == [
        "https://colorchase.meiyoutou.top",
        "https://meiyoutou.github.io",
        "http://localhost:5173",
        "https://example.com",
    ]


def test_cors_allows_non_browser_local_calls_without_origin(monkeypatch):
    monkeypatch.delenv("COLORCHASE_ALLOWED_ORIGINS", raising=False)

    response = _client().get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_default_and_env_browser_origins(monkeypatch):
    monkeypatch.setenv("COLORCHASE_ALLOWED_ORIGINS", "http://localhost:5173")
    client = _client()

    production_response = client.get(
        "/ping",
        headers={"Origin": "https://colorchase.meiyoutou.top"},
    )
    local_response = client.get(
        "/ping",
        headers={"Origin": "http://localhost:5173"},
    )

    assert production_response.status_code == 200
    assert production_response.headers["access-control-allow-origin"] == "https://colorchase.meiyoutou.top"
    assert local_response.status_code == 200
    assert local_response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_rejects_malicious_origin_and_preflight(monkeypatch):
    monkeypatch.delenv("COLORCHASE_ALLOWED_ORIGINS", raising=False)
    client = _client()

    simple_response = client.get(
        "/ping",
        headers={"Origin": "https://evil.example"},
    )
    preflight_response = client.options(
        "/ping",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert simple_response.status_code == 200
    assert "access-control-allow-origin" not in simple_response.headers
    assert preflight_response.status_code == 400
    assert "access-control-allow-origin" not in preflight_response.headers
