"""Authenticated dashboard with durable storage and HTTPS deployment support."""

import argparse
import csv
from collections import defaultdict
from decimal import Decimal
import io
import os
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile
import re

from flask import Flask, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import Auth, SESSION_SECONDS, ANONYMOUS_SECONDS
from storage import Store

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
CSV_COLUMNS = ["Seller", "Buyers Name", "Phone #", "email", "Street", "City", "# Bought", "$ Bought",
               "Payment Type", "Ball Numbers", "Notes", "Review Status", "Issues",
               "Source File", "Source Sheet", "Source Row"]


def money(value):
    return float(value.quantize(Decimal("0.01")))


def summary(store):
    result = store.result
    files = store.files()
    if result is None:
        return {"files": files, "leaderboard": [], "totals": {"tickets": 0, "revenue": 0, "purchases": 0, "sellers": 0, "review": 0}}
    groups, names = defaultdict(list), {}
    for seller in result.sellers:
        names.setdefault(seller.casefold(), seller)
        groups[seller.casefold()]
    for rec in result.records:
        names.setdefault(rec.seller.casefold(), rec.seller)
        groups[rec.seller.casefold()].append(rec)
    per_file = defaultdict(int)
    for rec in result.records:
        per_file[Path(rec.sourceFile).name] += 1
    for f in files:
        f["purchases"] = per_file.get(f["name"], 0)
    board = [{
        "seller": names[key],
        "tickets": sum(r.numberBought or 0 for r in recs),
        "revenue": money(sum((r.dollarBought for r in recs if r.dollarBought is not None), Decimal(0))),
        "purchases": len(recs),
        "review": sum(bool(r.issues) for r in recs),
    } for key, recs in groups.items()]
    board.sort(key=lambda s: (-s["tickets"], -s["revenue"], s["seller"].casefold()))
    return {
        "files": files,
        "leaderboard": board,
        "totals": {
            "tickets": sum(s["tickets"] for s in board),
            "revenue": money(sum((r.dollarBought for r in result.records if r.dollarBought is not None), Decimal(0))),
            "purchases": len(result.records),
            "sellers": len(board),
            "review": sum(bool(r.issues) for r in result.records),
        },
    }


def search(store, number):
    if store.result is None:
        return []
    return [{
        "name": r.name, "phone": r.phoneNumber, "email": r.email, "street": r.street, "city": r.city,
        "seller": r.seller, "tickets": r.numberBought, "numbers": r.numbers,
        "source": f"{Path(r.sourceFile).name} / {r.sourceSheet} row {r.sourceRow}", "issues": r.issues,
    } for r in store.result.records if number in r.numbers]


def safe_cell(value):
    """Stop spreadsheet apps from running buyer-entered text as a formula."""
    if isinstance(value, str) and value and (value[0] in "=@\t\r" or (
            value[0] in "+-" and not re.fullmatch(r"[+\-][\d\s().\-]*", value))):
        return "'" + value
    return value


def export_csv(store):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in store.result.records if store.result else []:
        writer.writerow([safe_cell(v) for v in [
            r.seller, r.name, r.phoneNumber, r.email, r.street, r.city,
            "" if r.numberBought is None else r.numberBought,
            "" if r.dollarBought is None else f"{r.dollarBought:.2f}",
            r.paymentType, ", ".join(map(str, r.numbers)), r.notes,
            "Needs review" if r.issues else "OK", " ".join(r.issues),
            Path(r.sourceFile).name, r.sourceSheet, r.sourceRow]])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8.


def create_app(data_dir=None, *, dev_http=False, public_origin=None, behind_proxy=False):
    if dev_http and behind_proxy:
        raise ValueError("Local HTTP mode cannot be used behind a public proxy.")
    origin = urlsplit(public_origin or os.environ.get("BALL_DROP_PUBLIC_ORIGIN", "https://localhost"))
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise ValueError("BALL_DROP_PUBLIC_ORIGIN must be an HTTPS origin, such as https://fundraiser.example.org.")
    store = Store(data_dir)
    auth = Auth(store.database)
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD,
                      TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"] if dev_http else [origin.hostname])
    app.extensions["store"] = store
    app.extensions["auth"] = auth
    cookie_name = "kw_session" if dev_http else "__Host-kw_session"
    app.config["AUTH_COOKIE_NAME"] = cookie_name
    if behind_proxy:
        # Exactly one proxy, Caddy; the backend must not have a public port.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)

    def cookie(response, token, authenticated):
        response.set_cookie(cookie_name, token, secure=not dev_http, httponly=True,
                            samesite="Lax", path="/",
                            max_age=SESSION_SECONDS if authenticated else ANONYMOUS_SECONDS)
        return response

    @app.before_request
    def protect_request():
        if dev_http:
            if request.remote_addr not in {"127.0.0.1", "::1"}:
                return jsonify(error="Local development access only."), 403
        elif not request.is_secure or request.host.casefold() != origin.netloc.casefold():
            return jsonify(error="Use the configured HTTPS address to access this dashboard."), 400
        token = request.cookies.get(cookie_name)
        g.login_session = auth.session(token)
        g.username = g.login_session["username"] if g.login_session else None
        public = request.path in {"/login", "/api/session", "/api/login"} or request.path.startswith("/assets/")
        if not public and not g.username:
            if request.path in {"/", "/index.html"}:
                return redirect("/login")
            return jsonify(error="Please sign in."), 401
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected_origin = request.host_url.rstrip("/") if dev_http else origin.geturl().rstrip("/")
            supplied_origin = request.headers.get("Origin")
            if supplied_origin and supplied_origin != expected_origin:
                return jsonify(error="Request origin does not match this dashboard."), 403
            supplied_csrf = request.headers.get("X-CSRF-Token", "")
            if (not g.login_session or not supplied_csrf
                    or not secrets.compare_digest(supplied_csrf, g.login_session["csrf_token"])):
                return jsonify(error="Your session changed. Refresh the page and try again."), 403

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if not dev_http:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/assets/<name>")
    def asset(name):
        if name not in {"dashboard.js", "dashboard.css", "login.js", "login.css"}:
            return jsonify(error="Not found."), 404
        return send_from_directory(STATIC, name)

    @app.get("/login")
    def login_page():
        if g.username:
            return redirect("/")
        return send_from_directory(STATIC, "login.html")

    @app.get("/")
    @app.get("/index.html")
    def dashboard():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/session")
    def session_info():
        current = g.login_session
        token = None
        if current is None:
            token, current = auth.issue_session()
        response = jsonify(authenticated=bool(current["username"]), username=current["username"],
                           csrfToken=current["csrf_token"])
        return cookie(response, token, False) if token else response

    @app.post("/api/login")
    def login():
        if not request.is_json or (request.content_length or 0) > 4096:
            return jsonify(error="Send a username and password as JSON."), 400
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error="Enter your username and password."), 400
        username, password = data.get("username"), data.get("password")
        if (not isinstance(username, str) or not isinstance(password, str)
                or not 1 <= len(username) <= 64 or not 1 <= len(password) <= 128):
            return jsonify(error="Enter your username and password."), 400
        user, retry_after = auth.authenticate(username, password, request.remote_addr or "unknown")
        if retry_after:
            return jsonify(error="Too many sign-in attempts. Try again in 15 minutes."), 429, {"Retry-After": str(retry_after)}
        if not user:
            return jsonify(error="Incorrect username or password."), 401
        token, current = auth.issue_session(user, request.cookies.get(cookie_name))
        return cookie(jsonify(authenticated=True, username=user, csrfToken=current["csrf_token"]), token, True)

    @app.post("/api/logout")
    def logout():
        auth.revoke(request.cookies.get(cookie_name))
        response = jsonify(ok=True)
        response.delete_cookie(cookie_name, path="/", secure=not dev_http, httponly=True, samesite="Lax")
        return response

    @app.get("/api/summary")
    def dashboard_summary():
        with store.lock:
            return jsonify(summary(store))

    @app.get("/api/search/<number>")
    def find_ball(number):
        if not re.fullmatch(r"[0-9]{1,12}", number) or int(number) < 1:
            return jsonify(error="Enter a positive ball number."), 400
        with store.lock:
            return jsonify(number=int(number), matches=search(store, int(number)))

    @app.get("/api/export.csv")
    def download_csv():
        with store.lock:
            body = export_csv(store)
        return app.response_class(body, mimetype="text/csv", headers={
            "Content-Disposition": 'attachment; filename="master_sales.csv"'})

    @app.post("/api/files")
    def upload_file():
        if not request.content_length:
            return jsonify(error="Choose a non-empty .xlsx file (25 MB maximum)."), 400
        try:
            name = store.add(unquote(request.headers.get("X-Filename", "")), request.get_data())
        except (OSError, ValueError, BadZipFile, KeyError) as exc:
            return jsonify(error=f"Could not read this spreadsheet: {exc}"), 400
        return jsonify(name=name), 201

    @app.delete("/api/files/<path:name>")
    def remove_file(name):
        try:
            store.remove(name)
        except FileNotFoundError:
            return jsonify(error="File not found."), 404
        except (OSError, ValueError, BadZipFile, KeyError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = "Files must be no larger than 25 MB." if exc.code == 413 else exc.description
        return jsonify(error=message), exc.code or 500

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        app.logger.error("Request failed (%s)", type(exc).__name__)
        return jsonify(error="The request could not be completed. Please try again."), 500

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the protected ball-drop dashboard.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", help="Persistent data folder (default: project/data)")
    parser.add_argument("--dev-http", action="store_true", help="Allow HTTP only on this computer for development")
    parser.add_argument("--behind-proxy", action="store_true", help="Trust exactly one HTTPS proxy; keep the backend port private")
    args = parser.parse_args(argv)
    if args.dev_http and args.host not in {"127.0.0.1", "::1"}:
        parser.error("--dev-http must bind to a loopback address.")
    if args.dev_http and args.behind_proxy:
        parser.error("--dev-http cannot be combined with --behind-proxy.")
    app = create_app(args.data_dir, dev_http=args.dev_http, behind_proxy=args.behind_proxy)
    if not app.extensions["auth"].has_users():
        parser.error("No staff accounts exist. Run python manage.py create-user USERNAME first (with the same --data-dir).")
    from waitress import serve
    print(f"Data saved in {app.extensions['store'].database.folder}")
    if args.dev_http:
        print(f"Local development: http://{args.host}:{args.port}")
    else:
        print("HTTPS required. Connect through the configured Caddy proxy.")
    serve(app, host=args.host, port=args.port, threads=4,
          max_request_body_size=MAX_UPLOAD, channel_timeout=30,
          clear_untrusted_proxy_headers=not args.behind_proxy)


if __name__ == "__main__":
    main()
