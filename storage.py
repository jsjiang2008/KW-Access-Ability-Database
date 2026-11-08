"""Durable SQLite storage for accounts, sessions, and original workbooks."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
from zipfile import ZipFile
import io

from workbooks import import_workbooks

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


class Database:
    def __init__(self, data_dir=None):
        self.folder = Path(data_dir or os.environ.get("BALL_DROP_DATA_DIR") or DEFAULT_DATA_DIR).resolve()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / "ball_drop.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT REFERENCES users(username) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    address TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    content BLOB NOT NULL
                );
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination == self.path:
            raise ValueError("Choose a backup path outside the live database.")
        # Exclusive creation prevents accidentally replacing an existing backup.
        with destination.open("xb"):
            pass
        try:
            with self.connect() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                    with target:
                        target.execute("DELETE FROM sessions")
                        target.execute("DELETE FROM login_attempts")
                finally:
                    target.close()
            if os.name != "nt":
                destination.chmod(0o600)
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def upload_name(filename):
    name = Path(filename.replace("\\", "/")).name.strip()
    if (not name.lower().endswith(".xlsx") or name.startswith("~$") or name.lower() == ".xlsx"
            or len(name) > 180 or any(ord(c) < 32 or c in '<>:"|?*' for c in name)):
        raise ValueError("Use an .xlsx filename without special path characters.")
    return name


def validate_archive(data):
    # XLSX is a ZIP archive. Bound expanded data before handing it to openpyxl.
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
            raise ValueError("This workbook is too large after decompression (100 MB maximum).")


class Store:
    """One application process, many threads; all mutations validate before commit."""

    def __init__(self, data_dir=None, database=None):
        self.database = database or Database(data_dir)
        self.lock = threading.RLock()
        self.result = None
        self.refresh()

    def files(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT name, length(content) AS size FROM uploads ORDER BY name COLLATE NOCASE")]

    def _uploads(self):
        with self.database.connect() as db:
            return {row["name"]: row["content"] for row in db.execute(
                "SELECT name, content FROM uploads ORDER BY name COLLATE NOCASE")}

    @staticmethod
    def _import(uploads):
        if not uploads:
            return None
        with tempfile.TemporaryDirectory(prefix="ball-drop-import-") as folder:
            paths = []
            for name, content in uploads.items():
                path = Path(folder) / name
                path.write_bytes(content)
                paths.append(path)
            result = import_workbooks(paths)
        # Preserve source names without exposing temporary server paths.
        for record in result.records:
            record.sourceFile = Path(record.sourceFile).name
        result.notices = [(Path(name).name, sheet, notice) for name, sheet, notice in result.notices]
        result.log = [(Path(name).name, sheet, count, message) for name, sheet, count, message in result.log]
        result.sources = {Path(name) for name in uploads}
        return result

    def refresh(self):
        with self.lock:
            self.result = self._import(self._uploads())

    def add(self, filename, data):
        name = upload_name(filename)
        validate_archive(data)
        with self.lock:
            uploads = self._uploads()
            old_name = next((key for key in uploads if key.casefold() == name.casefold()), None)
            if old_name is not None:
                del uploads[old_name]
            uploads[name] = data
            candidate = self._import(uploads)
            with self.database.connect() as db:
                if old_name is not None:
                    db.execute("DELETE FROM uploads WHERE name = ?", (old_name,))
                db.execute("INSERT INTO uploads(name, content) VALUES (?, ?)", (name, data))
            self.result = candidate
        return name

    def remove(self, filename):
        name = upload_name(filename)
        with self.lock:
            uploads = self._uploads()
            old_name = next((key for key in uploads if key.casefold() == name.casefold()), None)
            if old_name is None:
                raise FileNotFoundError(name)
            del uploads[old_name]
            candidate = self._import(uploads)
            with self.database.connect() as db:
                db.execute("DELETE FROM uploads WHERE name = ?", (old_name,))
            self.result = candidate
