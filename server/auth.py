"""Dashboard accounts: email + password (or Auth0, see auth0.py), cookie sessions.

The first account created on a fresh install is the admin; the admin adds teammates. Every /api route needs a
signed-in user except health, sign-in itself, and /ingest, which uses the project's API key instead.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .db import connect, now_iso

COOKIE = "dp_session"
SESSION_DAYS = 14
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login",
                "/api/auth/auth0/login", "/api/auth/callback"}
INGEST_PATH = re.compile(r"^/api/projects/[\w-]+/ingest$")

router = APIRouter(prefix="/api")


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=200)
    password: Optional[str] = Field(None, min_length=8, max_length=200)  # not needed when people sign in with Auth0
    role: Literal["admin", "member"] = "member"


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


_DUMMY_HASH = hash_password(secrets.token_hex(8))  # keeps unknown-email logins as slow as wrong-password ones


def _email(raw: str) -> str:
    email = raw.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        raise HTTPException(422, "Enter a valid email address")
    return email


def _public(row) -> Dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with connect() as db:
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                         "WHERE s.token_hash = ? AND s.expires_at > ?", (_token_hash(token), now_iso())).fetchone()
    return _public(row) if row else None


def needs_login(path: str, method: str) -> bool:
    return (path.startswith("/api/") and method != "OPTIONS" and path not in PUBLIC_PATHS
            and not INGEST_PATH.match(path))


def _start_session(response: Response, user_id: str) -> None:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                   (_token_hash(token), user_id, expires.replace(microsecond=0).isoformat()))
    # Secure cookies need HTTPS; turn them on with DATAPULSE_SECURE_COOKIES=1 when serving over HTTPS.
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=os.getenv("DATAPULSE_SECURE_COOKIES") == "1")


def _auth0() -> bool:
    from .auth0 import enabled  # auth0 imports this module
    return enabled()


def _passwords_only() -> None:
    if _auth0():
        raise HTTPException(409, "This DataPulse signs in with Auth0")


def _create_user(name: str, email: str, password: Optional[str], role: str) -> Dict[str, Any]:
    user = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "email": _email(email), "role": role}
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email = ?", (user["email"],)).fetchone():
            raise HTTPException(409, "Someone with that email already has an account")
        db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                   (user["id"], user["email"], user["name"], hash_password(password) if password else "", role, now_iso()))
    return user


def _admin(request: Request) -> Dict[str, Any]:
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "Only an admin can manage the team")
    return request.state.user


def _user_count() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"setup_needed": _user_count() == 0, "user": current_user(request), "auth0": _auth0()}


@router.post("/auth/setup")
def setup(payload: NewUser, response: Response) -> dict:
    """Create the first (admin) account. Only works while no account exists."""
    _passwords_only()
    if _user_count():
        raise HTTPException(409, "This DataPulse already has an admin; sign in instead")
    if not payload.password:
        raise HTTPException(422, "Choose a password of at least 8 characters")
    user = _create_user(payload.name, payload.email, payload.password, "admin")
    _start_session(response, user["id"])
    return user


@router.post("/auth/login")
def login(payload: Credentials, response: Response) -> dict:
    _passwords_only()
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (payload.email.strip().lower(),)).fetchone()
    if not verify_password(payload.password, row["password_hash"] if row else _DUMMY_HASH) or row is None:
        raise HTTPException(401, "Wrong email or password")
    _start_session(response, row["id"])
    return _public(row)


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE)
    if token:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    response.delete_cookie(COOKIE)
    from .auth0 import logout_url
    return {"signed_out": True, "redirect": logout_url(request)}


@router.put("/auth/password")
def change_password(payload: PasswordChange, request: Request) -> dict:
    _passwords_only()
    user_id = request.state.user["id"]
    with connect() as db:
        stored = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()["password_hash"]
        if not verify_password(payload.current_password, stored):
            raise HTTPException(403, "Your current password is not correct")
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(payload.new_password), user_id))
    return {"changed": True}


@router.get("/users")
def list_users(request: Request) -> list:
    _admin(request)
    with connect() as db:
        return [_public(r) for r in db.execute("SELECT * FROM users ORDER BY created_at")]


@router.post("/users")
def add_user(payload: NewUser, request: Request) -> dict:
    _admin(request)
    if not payload.password and not _auth0():
        raise HTTPException(422, "Give them a temporary password of at least 8 characters")
    return _create_user(payload.name, payload.email, payload.password, payload.role)


@router.delete("/users/{user_id}")
def remove_user(user_id: str, request: Request) -> dict:
    if _admin(request)["id"] == user_id:
        raise HTTPException(409, "You can't remove your own account")
    with connect() as db:
        if db.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount == 0:
            raise HTTPException(404, "User not found")
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return {"removed": True}
