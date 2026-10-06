"""Username/password authentication with role-based authorization.

Users are configured with the APP_USERS environment variable, a JSON object:

    {"alice": {"password_hash": "pbkdf2_sha256$...", "role": "admin"},
     "bob":   {"password_hash": "pbkdf2_sha256$...", "role": "user"}}

Generate a hash with:  python auth.py hash-password
"""

import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time

from fastapi import HTTPException, Request

log = logging.getLogger("trs.auth")

ROLES = {"user", "admin"}
PBKDF2_ITERATIONS = 600_000

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


# A real hash to verify against when the username doesn't exist, so response
# timing doesn't reveal which usernames are valid.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16), iterations=PBKDF2_ITERATIONS)


def load_users() -> dict:
    raw = os.environ.get("APP_USERS", "").strip()
    if not raw:
        log.warning("APP_USERS is not set; nobody will be able to log in.")
        return {}
    try:
        users = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"APP_USERS is not valid JSON: {e}")

    for username, info in users.items():
        if not isinstance(info, dict) or "password_hash" not in info:
            raise RuntimeError(f"APP_USERS entry for '{username}' needs a password_hash.")
        info.setdefault("role", "user")
        if info["role"] not in ROLES:
            raise RuntimeError(f"APP_USERS entry for '{username}' has unknown role '{info['role']}'.")
    return users


USERS = load_users()


class LoginThrottle:
    """Locks a username/IP pair out after repeated failed logins."""

    def __init__(self):
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - LOCKOUT_SECONDS
        attempts = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = attempts
        return attempts

    def is_locked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= MAX_FAILED_LOGINS

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


throttle = LoginThrottle()


def authenticate(username: str, password: str) -> dict | None:
    user = USERS.get(username)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return {"username": username, "role": user["role"]}


def current_user(request: Request) -> dict | None:
    user = request.session.get("user")
    # Drop sessions for users who have since been removed from APP_USERS.
    if user and user.get("username") in USERS:
        return {"username": user["username"], "role": USERS[user["username"]]["role"]}
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in again.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


if __name__ == "__main__":
    if sys.argv[1:] != ["hash-password"]:
        sys.exit("usage: python auth.py hash-password")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm:  "):
        sys.exit("Passwords don't match.")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    print(hash_password(password))
