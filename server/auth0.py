"""Sign in with Auth0 (Google, GitHub, email and more, configured in Auth0).

Turned on by AUTH0_DOMAIN, AUTH0_CLIENT_ID and AUTH0_CLIENT_SECRET. Auth0 proves who someone is; DataPulse still
decides who gets in: the first person to sign in on a fresh install becomes the admin, and after that only people
the admin added (matched by verified email). Signing in creates the same session cookie as the password sign-in.

The flow is OpenID Connect's authorization code flow with PKCE, state and nonce. The ID token comes straight from
Auth0's token endpoint over TLS using the client secret, so its claims are checked (issuer, audience, expiry, nonce)
without verifying its signature (OpenID Connect Core 3.1.3.7).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .auth import _start_session, _user_count
from .db import connect, now_iso

FLOW_COOKIE = "dp_auth0"
FLOW_SECONDS = 600
CALLBACK = "/api/auth/callback"

router = APIRouter(prefix="/api")


class SignInError(Exception):
    """A reason sign-in failed, safe to show on the sign-in page."""


def settings() -> Optional[Dict[str, str]]:
    domain = os.getenv("AUTH0_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
    client_id, secret = os.getenv("AUTH0_CLIENT_ID", "").strip(), os.getenv("AUTH0_CLIENT_SECRET", "").strip()
    if not (domain and client_id and secret):
        return None
    return {"domain": domain, "client_id": client_id, "client_secret": secret}


def enabled() -> bool:
    return settings() is not None


def _site(request: Request) -> str:
    """This site's address, e.g. https://datapulse.example.com (uvicorn runs with --proxy-headers behind Railway)."""
    return str(request.base_url).rstrip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(id_token: str, cfg: Dict[str, str], nonce: str) -> Dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise SignInError("Auth0 sent back an unreadable sign-in. Please try again.")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if (claims.get("iss") != f"https://{cfg['domain']}/" or cfg["client_id"] not in audiences
            or claims.get("exp", 0) < time.time() or claims.get("nonce") != nonce):
        raise SignInError("That sign-in couldn't be verified. Please try again.")
    return claims


def _user_for(claims: Dict[str, Any]) -> str:
    """The DataPulse user id for a verified Auth0 identity; creates the admin on a fresh install."""
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise SignInError("Auth0 didn't share an email address, so DataPulse can't tell who you are.")
    if not claims.get("email_verified"):
        raise SignInError(f"Verify {email} first: open the email from Auth0, click the link, then sign in again.")
    with connect() as db:
        row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            return row["id"]
    if _user_count():
        raise SignInError(f"{email} doesn't have access to this DataPulse yet. Ask your admin to add you.")
    user_id = uuid.uuid4().hex[:12]
    name = str(claims.get("name") or claims.get("nickname") or email.split("@")[0])[:80]
    with connect() as db:  # empty password hash: this account can only sign in through Auth0
        db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)", (user_id, email, name, "", "admin", now_iso()))
    return user_id


def _back_to_sign_in(message: str) -> RedirectResponse:
    response = RedirectResponse("/?" + urlencode({"signin_error": message}), status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/api/auth")
    return response


@router.get("/auth/auth0/login")
def start(request: Request):
    cfg = settings()
    if not cfg:
        return _back_to_sign_in("Auth0 isn't set up on this server.")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    query = urlencode({
        "response_type": "code", "client_id": cfg["client_id"], "redirect_uri": _site(request) + CALLBACK,
        "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    })
    response = RedirectResponse(f"https://{cfg['domain']}/authorize?{query}", status_code=303)
    response.set_cookie(FLOW_COOKIE, ".".join((state, nonce, verifier)), max_age=FLOW_SECONDS, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/api/auth")
    return response


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    cfg = settings()
    if not cfg:
        return _back_to_sign_in("Auth0 isn't set up on this server.")
    if error:
        return _back_to_sign_in(error_description or "Sign-in was cancelled.")
    try:
        expected, nonce, verifier = request.cookies.get(FLOW_COOKIE, "").split(".")
    except ValueError:
        return _back_to_sign_in("The sign-in took too long or started in another browser. Please try again.")
    if not code or not secrets.compare_digest(state, expected):
        return _back_to_sign_in("That sign-in couldn't be verified. Please try again.")
    try:
        reply = httpx.post(f"https://{cfg['domain']}/oauth/token", timeout=15, data={
            "grant_type": "authorization_code", "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "code": code, "redirect_uri": _site(request) + CALLBACK, "code_verifier": verifier,
        })
        if reply.status_code != 200:
            raise SignInError("Auth0 didn't accept the sign-in. Check the app's Client ID and Secret on the server.")
        user_id = _user_for(_claims(reply.json().get("id_token", ""), cfg, nonce))
    except SignInError as e:
        return _back_to_sign_in(str(e))
    except httpx.HTTPError:
        return _back_to_sign_in("Couldn't reach Auth0. Please try again.")
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/api/auth")
    _start_session(response, user_id)
    return response


def logout_url(request: Request) -> Optional[str]:
    """Where to send the browser so Auth0 forgets the sign-in too; None when Auth0 is off."""
    cfg = settings()
    if not cfg:
        return None
    return f"https://{cfg['domain']}/v2/logout?" + urlencode({"client_id": cfg["client_id"], "returnTo": _site(request)})
