"""Staff accounts and revocable sessions; only token hashes are stored on disk."""

import hashlib
import re
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

SESSION_SECONDS = 8 * 60 * 60
ANONYMOUS_SECONDS = 15 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_LIMIT = 5


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def account_name(value):
    name = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", name):
        raise ValueError("Usernames must be 3–64 characters: letters, numbers, dots, underscores or hyphens.")
    return name


class Auth:
    def __init__(self, database):
        self.database = database
        self._dummy_hash = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")

    def has_users(self):
        with self.database.connect() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def set_password(self, username, password, create=False):
        username = account_name(username)
        if not 12 <= len(password) <= 128:
            raise ValueError("Use a password or passphrase between 12 and 128 characters.")
        password_hash = generate_password_hash(password, method="scrypt")
        with self.database.connect() as db:
            exists = db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if create and exists:
                raise ValueError("That account already exists. Use reset-password to change its password.")
            if not create and not exists:
                raise ValueError("Account not found.")
            db.execute("INSERT INTO users(username, password_hash) VALUES (?, ?) "
                       "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
                       (username, password_hash))
            db.execute("DELETE FROM sessions WHERE username = ?", (username,))

    def delete_user(self, username):
        with self.database.connect() as db:
            cursor = db.execute("DELETE FROM users WHERE username = ?", (account_name(username),))
            if cursor.rowcount == 0:
                raise ValueError("Account not found.")

    def issue_session(self, username=None, previous_token=None):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        ttl = SESSION_SECONDS if username else ANONYMOUS_SECONDS
        now = time.time()
        with self.database.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            if previous_token:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(previous_token),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (token_hash(token), username, csrf, now + ttl))
        return token, {"username": username, "csrf_token": csrf, "expires_at": now + ttl}

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.database.connect() as db:
            row = db.execute("SELECT username, csrf_token, expires_at FROM sessions "
                             "WHERE token_hash = ? AND expires_at > ?", (token_hash(token), time.time())).fetchone()
            return dict(row) if row else None

    def revoke(self, token):
        if token:
            with self.database.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def authenticate(self, username, password, address):
        """Reserve an attempt transactionally; failures survive application restarts."""
        now = time.time()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE expires_at <= ?", (now,))
            row = db.execute("SELECT attempts, expires_at FROM login_attempts WHERE address = ?", (address,)).fetchone()
            if row and row["attempts"] >= LOGIN_LIMIT:
                return None, max(1, int(row["expires_at"] - now))
            db.execute("INSERT INTO login_attempts VALUES (?, 1, ?) "
                       "ON CONFLICT(address) DO UPDATE SET attempts=attempts+1", (address, now + LOGIN_WINDOW))
            user = db.execute("SELECT username, password_hash FROM users WHERE username = ?",
                              (username.strip().casefold(),)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else self._dummy_hash, password)
        if valid and user:
            with self.database.connect() as db:
                db.execute("DELETE FROM login_attempts WHERE address = ?", (address,))
            return user["username"], 0
        return None, 0
