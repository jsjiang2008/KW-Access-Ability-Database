"""Local web dashboard: upload seller workbooks, view totals, search balls, export CSV.

Run `python server.py`, then open http://127.0.0.1:8000. Uploaded files are kept
in a temporary folder that is deleted when the server stops.
"""

import argparse
import csv
from collections import defaultdict
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import unquote, urlparse
from zipfile import BadZipFile

from workbooks import import_workbooks

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
CSV_COLUMNS = ["Seller", "Buyers Name", "Phone #", "email", "Street", "City", "# Bought", "$ Bought",
               "Payment Type", "Ball Numbers", "Notes", "Review Status", "Issues",
               "Source File", "Source Sheet", "Source Row"]


class Store:
    """Uploaded workbooks plus the combined import of all of them."""

    def __init__(self):
        self.folder = tempfile.TemporaryDirectory(prefix="ball-drop-")
        self.lock = threading.Lock()
        self.result = None

    def files(self):
        return sorted(Path(self.folder.name).glob("*.xlsx"), key=lambda p: p.name.casefold())

    def refresh(self):
        files = self.files()
        self.result = import_workbooks(files) if files else None

    def add(self, filename, data):
        name = Path(filename.replace("\\", "/")).name.strip()
        if not name.lower().endswith(".xlsx") or name.startswith("~$") or name in {".xlsx"}:
            raise ValueError("Only .xlsx spreadsheets can be uploaded.")
        with self.lock:
            target = Path(self.folder.name) / name
            staging = Path(self.folder.name) / "staging"
            staging.mkdir(exist_ok=True)
            check = staging / name
            check.write_bytes(data)
            try:
                import_workbooks([check])  # Reject unreadable files before replacing anything.
                check.replace(target)
            finally:
                check.unlink(missing_ok=True)
            self.refresh()
        return name

    def remove(self, filename):
        with self.lock:
            target = Path(self.folder.name) / Path(filename).name
            if not target.is_file():
                raise FileNotFoundError(filename)
            target.unlink()
            self.refresh()


def money(value):
    return float(value.quantize(Decimal("0.01")))


def summary(store):
    result = store.result
    files = [{"name": p.name, "size": p.stat().st_size} for p in store.files()]
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


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json", headers=None):
            if content_type == "application/json":
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, message):
            self.send(status, {"error": message})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in {"/", "/index.html"}:
                self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/summary":
                with store.lock:
                    self.send(200, summary(store))
            elif url.path.startswith("/api/search/"):
                raw = unquote(url.path.removeprefix("/api/search/")).strip()
                if not raw.isdigit() or int(raw) < 1:
                    return self.error(400, "Enter a positive ball number.")
                with store.lock:
                    self.send(200, {"number": int(raw), "matches": search(store, int(raw))})
            elif url.path == "/api/export.csv":
                with store.lock:
                    body = export_csv(store)
                self.send(200, body, "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="master_sales.csv"'})
            else:
                self.error(404, "Not found.")

        def do_POST(self):
            if urlparse(self.path).path != "/api/files":
                return self.error(404, "Not found.")
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD:
                return self.error(413, "Files must be between 1 byte and 25 MB.")
            data = self.rfile.read(length)
            try:
                name = store.add(unquote(self.headers.get("X-Filename", "")), data)
            except (OSError, ValueError, BadZipFile, KeyError) as exc:
                return self.error(400, f"Could not read this spreadsheet: {exc}")
            self.send(201, {"name": name})

        def do_DELETE(self):
            url = urlparse(self.path)
            if not url.path.startswith("/api/files/"):
                return self.error(404, "Not found.")
            try:
                store.remove(unquote(url.path.removeprefix("/api/files/")))
            except FileNotFoundError:
                return self.error(404, "File not found.")
            except (OSError, ValueError, BadZipFile, KeyError) as exc:
                return self.error(400, str(exc))
            self.send(200, {"ok": True})

        def log_message(self, format: str, *args: object) -> None:
            pass  # Keep request paths, including ball-number searches, out of the terminal.

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the ball-drop sales dashboard.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    store = Store()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"Dashboard running at http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.folder.cleanup()


if __name__ == "__main__":
    main()
