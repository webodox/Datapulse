"""Sign in with Auth0 (server/auth0.py), with Auth0's token endpoint faked."""
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import app as server  # noqa: E402
from server import auth0  # noqa: E402

DOMAIN, CLIENT = "dev-test.us.auth0.com", "client-123"


class FakeReply:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _token(claims):
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setenv("AUTH0_CLIENT_ID", CLIENT)
    monkeypatch.setenv("AUTH0_CLIENT_SECRET", "shh")
    with TestClient(server.app) as c:
        yield c


def sign_in(client, monkeypatch, email, verified=True, nonce=None, state=None, **extra):
    """Run the whole round trip; returns the final redirect target."""
    started = client.get("/api/auth/auth0/login", follow_redirects=False)
    assert started.status_code == 303
    to = urlsplit(started.headers["location"])
    query = {k: v[0] for k, v in parse_qs(to.query).items()}
    assert (to.netloc, to.path) == (DOMAIN, "/authorize")
    assert query["redirect_uri"] == "http://testserver/api/auth/callback" and query["code_challenge_method"] == "S256"
    claims = {"iss": f"https://{DOMAIN}/", "aud": CLIENT, "exp": time.time() + 60, "nonce": nonce or query["nonce"],
              "email": email, "email_verified": verified, "name": "Grace", **extra}
    sent = {}

    def fake_post(url, data, timeout):
        sent.update(data, url=url)
        return FakeReply(200, {"id_token": _token(claims)})
    monkeypatch.setattr(auth0.httpx, "post", fake_post)
    back = client.get("/api/auth/callback", params={"code": "abc", "state": state or query["state"]},
                      follow_redirects=False)
    assert back.status_code == 303
    if sent:
        assert sent["url"] == f"https://{DOMAIN}/oauth/token" and sent["code_verifier"] and sent["client_secret"] == "shh"
    return back.headers["location"]


def _error(location):
    return parse_qs(urlsplit(location).query).get("signin_error", [""])[0]


def test_first_person_becomes_admin_and_others_need_an_invite(client, monkeypatch):
    status = client.get("/api/auth/status").json()
    assert status["auth0"] is True and status["setup_needed"] is True
    assert sign_in(client, monkeypatch, "Grace@Example.com") == "/"
    me = client.get("/api/auth/status").json()["user"]
    assert me["email"] == "grace@example.com" and me["role"] == "admin"

    # an admin adds teammates by email only; they get in through Auth0
    assert client.post("/api/users", json={"name": "Lin", "email": "lin@example.com"}).status_code == 200
    client.cookies.clear()
    assert "doesn't have access" in _error(sign_in(client, monkeypatch, "stranger@example.com"))
    assert client.get("/api/auth/status").json()["user"] is None
    assert sign_in(client, monkeypatch, "lin@example.com") == "/"
    assert client.get("/api/auth/status").json()["user"]["role"] == "member"


def test_passwords_are_off_while_auth0_is_on(client, monkeypatch):
    person = {"name": "Eve", "email": "eve@example.com", "password": "correct horse"}
    assert client.post("/api/auth/setup", json=person).status_code == 409
    sign_in(client, monkeypatch, "grace@example.com")
    assert client.put("/api/auth/password", json={"current_password": "", "new_password": "longenough"}).status_code == 409
    client.post("/api/users", json=person)
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"email": person["email"], "password": person["password"]}).status_code == 409


def test_unverified_email_and_tampering_are_refused(client, monkeypatch):
    assert "Verify" in _error(sign_in(client, monkeypatch, "grace@example.com", verified=False))
    assert "couldn't be verified" in _error(sign_in(client, monkeypatch, "grace@example.com", nonce="replayed"))
    assert "couldn't be verified" in _error(sign_in(client, monkeypatch, "grace@example.com", state="forged"))
    assert "couldn't be verified" in _error(sign_in(client, monkeypatch, "grace@example.com", aud="someone-else"))
    assert "couldn't be verified" in _error(sign_in(client, monkeypatch, "grace@example.com", exp=time.time() - 5))
    assert client.get("/api/auth/status").json()["user"] is None


def test_callback_without_a_started_sign_in_is_refused(client):
    back = client.get("/api/auth/callback", params={"code": "abc", "state": "x"}, follow_redirects=False)
    assert "try again" in _error(back.headers["location"])
    cancelled = client.get("/api/auth/callback", params={"error": "access_denied", "error_description": "User cancelled"},
                           follow_redirects=False)
    assert _error(cancelled.headers["location"]) == "User cancelled"


def test_sign_out_also_signs_out_of_auth0(client, monkeypatch):
    sign_in(client, monkeypatch, "grace@example.com")
    redirect = client.post("/api/auth/logout").json()["redirect"]
    assert redirect.startswith(f"https://{DOMAIN}/v2/logout?") and "returnTo=http%3A%2F%2Ftestserver" in redirect
    assert client.get("/api/auth/status").json()["user"] is None


def test_without_auth0_settings_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "home"))
    for name in ("AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(server.app) as c:
        assert c.get("/api/auth/status").json()["auth0"] is False
        assert c.post("/api/auth/setup", json={"name": "A", "email": "a@example.com"}).status_code == 422
        assert c.post("/api/auth/setup", json={"name": "A", "email": "a@example.com", "password": "correct horse"}).status_code == 200
        assert c.post("/api/users", json={"name": "B", "email": "b@example.com"}).status_code == 422
        assert c.post("/api/auth/logout").json()["redirect"] is None
