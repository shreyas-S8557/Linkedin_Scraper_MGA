from __future__ import annotations


def test_test_connection_reports_not_configured_when_env_missing(client, monkeypatch):
    monkeypatch.delenv("MAILERLITE_API_TOKEN", raising=False)
    monkeypatch.delenv("MAILERLITE_SENDER_EMAIL", raising=False)
    monkeypatch.delenv("MAILERLITE_SENDER_NAME", raising=False)
    r = client.post("/api/email/provider/test")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "mailerlite"
    assert body["configured"] is False
    assert body["authenticated"] is False
    assert "api_token" not in body


def test_test_connection_never_leaks_token_even_if_set(client, monkeypatch):
    monkeypatch.setenv("MAILERLITE_API_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("MAILERLITE_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("MAILERLITE_SENDER_NAME", "Sender")
    r = client.post("/api/email/provider/test")
    assert r.status_code == 200
    assert "super-secret-token-value" not in r.text


def test_test_send_requires_to_address(client):
    r = client.post("/api/email/provider/test-send", json={"to": "not-an-email"})
    assert r.status_code == 422  # pydantic EmailStr validation


def test_test_send_without_credentials_fails_cleanly_not_500(client, monkeypatch):
    monkeypatch.delenv("MAILERLITE_API_TOKEN", raising=False)
    monkeypatch.delenv("MAILERLITE_SENDER_EMAIL", raising=False)
    monkeypatch.delenv("MAILERLITE_SENDER_NAME", raising=False)
    r = client.post("/api/email/provider/test-send", json={"to": "someone@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "MAILERLITE_API_TOKEN" in body["error"]
