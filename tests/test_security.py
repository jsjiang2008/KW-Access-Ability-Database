"""Authentication, persistence and production transport regression checks."""

import csv
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from server import create_app
from storage import Database, Store
from test_server import purchase, workbook_bytes


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name, dev_http=True)
        self.auth = self.app.extensions["auth"]
        self.store = self.app.extensions["store"]
        self.password = "correct test passphrase"
        self.auth.set_password("staff", self.password, create=True)
        self.client = self.app.test_client()

    def login(self, client=None, password=None):
        client = client or self.client
        csrf = client.get("/api/session").json["csrfToken"]
        response = client.post("/api/login", json={"username": "staff", "password": password or self.password},
                               headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.data)
        return response.json["csrfToken"]

    def upload(self, csrf):
        return self.client.post("/api/files", data=workbook_bytes([purchase()]),
                                headers={"X-Filename": "Sales.xlsx", "X-CSRF-Token": csrf})

    def test_every_buyer_endpoint_requires_login(self):
        for method, path in [("GET", "/api/summary"), ("GET", "/api/search/101"),
                             ("GET", "/api/export.csv"), ("POST", "/api/files"),
                             ("DELETE", "/api/files/Sales.xlsx"), ("POST", "/api/logout")]:
            with self.subTest(path=path):
                response = self.client.open(path, method=method)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/").location, "/login")

    def test_login_rotates_session_and_logout_revokes_it(self):
        self.client.get("/api/session")
        before = self.client.get_cookie("kw_session").value
        csrf = self.login()
        after = self.client.get_cookie("kw_session").value
        self.assertNotEqual(before, after)
        self.assertIsNone(self.auth.session(before))
        self.assertEqual(self.client.get("/api/summary").status_code, 200)
        self.assertEqual(self.client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.client.set_cookie("kw_session", after)
        self.assertEqual(self.client.get("/api/export.csv").status_code, 401)

    def test_csrf_required_for_login_upload_delete_and_logout(self):
        self.client.get("/api/session")
        response = self.client.post("/api/login", json={"username": "staff", "password": self.password})
        self.assertEqual(response.status_code, 403)
        csrf = self.login()
        self.assertEqual(self.upload(csrf).status_code, 201)
        for method, path in [("POST", "/api/files"), ("DELETE", "/api/files/Sales.xlsx"), ("POST", "/api/logout")]:
            self.assertEqual(self.client.open(path, method=method).status_code, 403)
        self.assertEqual(len(self.store.files()), 1)

    def test_cross_origin_mutation_is_rejected_even_with_csrf(self):
        csrf = self.login()
        response = self.client.post("/api/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/summary").status_code, 200)

    def test_login_rate_limit_survives_restart(self):
        csrf = self.client.get("/api/session").json["csrfToken"]
        for _ in range(5):
            response = self.client.post("/api/login", json={"username": "staff", "password": "incorrect"},
                                        headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 401)
        restarted = create_app(self.directory.name, dev_http=True).test_client()
        csrf = restarted.get("/api/session").json["csrfToken"]
        blocked = restarted.post("/api/login", json={"username": "staff", "password": self.password},
                                 headers={"X-CSRF-Token": csrf})
        self.assertEqual(blocked.status_code, 429)
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)

    def test_password_reset_and_account_removal_revoke_sessions(self):
        self.login()
        self.auth.set_password("staff", "new correct test passphrase")
        self.assertEqual(self.client.get("/api/summary").status_code, 401)
        self.login(password="new correct test passphrase")
        self.auth.delete_user("staff")
        self.assertEqual(self.client.get("/api/summary").status_code, 401)

    def test_expired_session_is_rejected(self):
        self.login()
        with self.store.database.connect() as db:
            db.execute("UPDATE sessions SET expires_at=0")
        self.assertEqual(self.client.get("/api/search/101").status_code, 401)

    def test_passwords_and_session_tokens_are_hashed(self):
        self.login()
        token = self.client.get_cookie("kw_session").value
        with self.store.database.connect() as db:
            password_hash = db.execute("SELECT password_hash FROM users").fetchone()[0]
            tokens = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
        self.assertTrue(password_hash.startswith("scrypt:"))
        self.assertNotIn(self.password, password_hash)
        self.assertNotIn(token, tokens)

    def test_uploads_accounts_and_sessions_survive_restart(self):
        csrf = self.login()
        self.assertEqual(self.upload(csrf).status_code, 201)
        token = self.client.get_cookie("kw_session").value
        restarted_app = create_app(self.directory.name, dev_http=True)
        restarted = restarted_app.test_client()
        restarted.set_cookie("kw_session", token)
        summary = restarted.get("/api/summary").json
        self.assertEqual(summary["totals"]["tickets"], 3)
        self.assertEqual(summary["files"][0]["name"], "Sales.xlsx")
        self.assertEqual(restarted.get("/api/search/101").json["matches"][0]["name"], "Buyer")
        rows = list(csv.DictReader(io.StringIO(restarted.get("/api/export.csv").data.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["Seller"], "Alex")
        self.assertEqual(rows[0]["Source File"], "Sales.xlsx")

    def test_invalid_upload_preserves_persistent_workbook(self):
        csrf = self.login()
        self.assertEqual(self.upload(csrf).status_code, 201)
        response = self.client.post("/api/files", data=b"not a workbook",
                                    headers={"X-Filename": "Sales.xlsx", "X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 400)
        restarted = Store(self.directory.name)
        self.assertEqual(restarted.result.records[0].name, "Buyer")

    def test_backup_restores_workbooks_and_accounts_without_sessions(self):
        csrf = self.login()
        self.upload(csrf)
        destination = Path(self.directory.name) / "backup.sqlite3"
        self.store.database.backup(destination)
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM uploads").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        with self.assertRaises(FileExistsError):
            self.store.database.backup(destination)

    def test_validation_failure_does_not_change_stored_or_cached_data(self):
        self.store.add("Sales.xlsx", workbook_bytes([purchase()]))
        original = self.store.result
        with patch.object(Store, "_import", side_effect=ValueError("bad input")):
            with self.assertRaises(ValueError):
                self.store.add("Sales.xlsx", workbook_bytes([purchase("Replacement")]))
        self.assertIs(self.store.result, original)
        self.assertEqual(Store(self.directory.name).result.records[0].name, "Buyer")

    def test_production_requires_https_and_ignores_untrusted_forwarded_headers(self):
        app = create_app(self.directory.name, public_origin="https://fundraiser.example.org")
        client = app.test_client()
        self.assertEqual(client.get("/api/session", base_url="http://fundraiser.example.org").status_code, 400)
        self.assertEqual(client.get("/api/session", base_url="http://fundraiser.example.org",
                                    headers={"X-Forwarded-Proto": "https"}).status_code, 400)
        response = client.get("/api/session", base_url="https://fundraiser.example.org")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        for flag in ["__Host-kw_session=", "Secure", "HttpOnly", "SameSite=Lax", "Path=/"]:
            self.assertIn(flag, cookie)
        self.assertIn("max-age=", response.headers["Strict-Transport-Security"])
        self.assertEqual(client.get("/api/session", base_url="https://attacker.invalid").status_code, 400)

    def test_explicit_single_proxy_mode_accepts_caddy_https_header(self):
        client = create_app(self.directory.name, public_origin="https://fundraiser.example.org",
                            behind_proxy=True).test_client()
        response = client.get("/api/session", base_url="http://fundraiser.example.org",
                              headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/session", base_url="http://fundraiser.example.org").status_code, 400)

    def test_local_http_mode_rejects_remote_clients_and_proxy_mode(self):
        response = self.client.get("/api/session", environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(ValueError):
            create_app(self.directory.name, dev_http=True, behind_proxy=True)

    def test_security_headers_and_upload_size_limit(self):
        csrf = self.login()
        response = self.client.get("/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("<script>", response.get_data(as_text=True))
        response.close()
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        response = self.client.post("/api/files", data=b"a" * 101,
                                    headers={"X-Filename": "Sales.xlsx", "X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
