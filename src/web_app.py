"""Local browser-based FileBox application."""
from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import shutil
import tempfile
import threading
import time
import webbrowser
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import mysql.connector
from docx import Document
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from openpyxl import load_workbook
from waitress import serve
from werkzeug.exceptions import HTTPException

import database
import os_backend
from access_scheduler import ReaderWriterScheduler
from security import hash_password, safe_name, safe_storage_path, verify_password


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = PROJECT_ROOT / "storage"
TEMP_ROOT = PROJECT_ROOT / "work" / "uploads"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
EDIT_LOCK_SECONDS = 45

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FILEBOX_SECRET_KEY") or secrets.token_bytes(32)
app.config.update(
    MAX_CONTENT_LENGTH=max(1, int(os.environ.get("FILEBOX_MAX_UPLOAD_MB", "1024"))) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)
logging.basicConfig(level=os.environ.get("FILEBOX_LOG_LEVEL", "INFO"))

_schema_lock = threading.Lock()
_schema_ready = False
_audit_queue: queue.Queue[dict] = queue.Queue()
_audit_thread_started = False
_audit_thread_lock = threading.Lock()


def _audit_worker():
    while True:
        event = _audit_queue.get()
        try:
            conn = database.connect()
            try:
                user_id, file_id = event.get("user_id"), event.get("file_id")
                cursor = conn.cursor()
                if file_id is not None:
                    # Hold a shared row lock through the activity insert so a
                    # concurrent delete cannot race the activity-log FK.
                    cursor.execute("SELECT 1 FROM files WHERE file_id=%s LOCK IN SHARE MODE", (file_id,))
                    if cursor.fetchone() is None:
                        file_id = None
                if user_id is not None:
                    cursor.execute("SELECT 1 FROM users WHERE user_id=%s LOCK IN SHARE MODE", (user_id,))
                    if cursor.fetchone() is None:
                        user_id = None
                cursor.close()
                details = json.dumps(event.get("details", {}), ensure_ascii=False, separators=(",", ":"))
                database.log_activity(conn, user_id, file_id,
                                      event.get("action", "ACCESS_EVENT"), details)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            app.logger.exception("Could not persist an access scheduling event")
        finally:
            _audit_queue.task_done()


def _ensure_audit_worker():
    global _audit_thread_started
    with _audit_thread_lock:
        if not _audit_thread_started:
            threading.Thread(target=_audit_worker, name="filebox-audit", daemon=True).start()
            _audit_thread_started = True


def _enqueue_access_event(event: dict):
    _ensure_audit_worker()
    _audit_queue.put(dict(event, details={k: v for k, v in event.items()
                                         if k not in {"action", "user_id", "file_id"}}))


SCHEDULER = ReaderWriterScheduler(
    aging_interval=float(os.environ.get("FILEBOX_PRIORITY_AGING_SECONDS", "15")),
    event_sink=_enqueue_access_event,
)


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if not _schema_ready:
            database.initialize()
            STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
            _schema_ready = True


def db():
    if "db" not in g:
        g.db = database.connect()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None and connection.is_connected():
        connection.close()


def _csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def template_globals():
    return {"csrf_token": _csrf_token, "current_user": getattr(g, "current_user", None)}


@app.before_request
def prepare_request():
    if request.endpoint != "static":
        _ensure_schema()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.form.get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, session.get("_csrf_token", "")):
            abort(400, description=(
                "This form expired or your account changed in another tab. FileBox uses one account per browser profile; "
                "use separate browser profiles (or a private window) to test two accounts, then reload this page."
            ))


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data:; media-src 'self'; frame-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login", next=request.path))
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT user_id, username, email FROM users WHERE user_id=%s", (user_id,))
        g.current_user = cursor.fetchone()
        cursor.close()
        if not g.current_user:
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def _audit(user_id, file_id, action, details=""):
    database.log_activity(db(), user_id, file_id, action, str(details)[:500])
    db().commit()


def _deny(file_id, action="ACCESS_DENIED"):
    user_id = session.get("user_id")
    _audit(user_id, file_id, action, "Permission check denied the request")
    abort(403)


def _file_access(file_id: int, user_id: int):
    cursor = db().cursor(dictionary=True)
    cursor.execute(
        """SELECT f.*, owner.username AS owner_name,
                  p.permission_type AS user_permission, p.allow_download AS user_download
           FROM files f JOIN users owner ON owner.user_id=f.owner_id
           LEFT JOIN permissions p ON p.file_id=f.file_id AND p.grantee_user_id=%s
           WHERE f.file_id=%s""",
        (user_id, file_id),
    )
    row = cursor.fetchone()
    cursor.close()
    if not row:
        abort(404)
    owner = row["owner_id"] == user_id
    permission = row["user_permission"]
    mode = row.get("access_mode", "PRIVATE")
    authenticated = mode in {"AUTH_READ", "AUTH_WRITE"}
    can_read = owner or permission in {"READ", "WRITE"} or authenticated
    can_write = owner or permission == "WRITE" or mode == "AUTH_WRITE"
    can_download = owner or (permission is not None and bool(row["user_download"])) or (
        authenticated and bool(row.get("allow_authenticated_download", 1))
    )
    row["is_owner"] = owner
    row.update(is_owner=owner, can_read=can_read, can_write=can_write,
               can_download=can_download, access_label=_access_label(row))
    return row


def _access_label(row):
    if row["is_owner"]:
        return "Owner"
    if row.get("user_permission"):
        return row["user_permission"].title()
    mode = row.get("access_mode")
    return {"AUTH_READ": "Open · read", "AUTH_WRITE": "Open · write"}.get(mode, "Private")


def _require_file(file_id: int, mode: str | None = None):
    info = _file_access(file_id, g.current_user["user_id"])
    if mode == "read" and not info["can_read"]:
        _deny(file_id)
    if mode == "write" and not info["can_write"]:
        _deny(file_id, "WRITE_DENIED")
    return info


def _priority():
    raw = request.form.get("priority", request.args.get("priority", "1"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return value if value in {0, 1, 2} else 1


def _mime_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _decode_text_file(raw: bytes) -> str:
    """Decode common text encodings, including BOM-marked Windows files."""
    encodings = (
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\xfe\xff", "utf-16"),
        (b"\xff\xfe", "utf-16"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    )
    for bom, encoding in encodings:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Older Windows editors often save plain-text files as the active
        # Windows code page (commonly CP-1252) instead of UTF-8.
        return raw.decode("cp1252", errors="replace")


def _get_file_bytes(info, *, priority=1):
    lease = SCHEDULER.acquire(info["file_id"], "read", priority=priority,
                              user_id=g.current_user["user_id"], file_id=info["file_id"])
    with lease:
        latest = _require_file(info["file_id"], "read")
        path = safe_storage_path(STORAGE_ROOT, latest["owner_id"], latest["relative_path"])
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("This file is too large for an in-page text preview.")
        return path.read_bytes()


def _claim_edit_lock(file_id: int, user_id: int):
    """Claim the per-file edit lease, reusing a live lease for the same user."""
    cursor = db().cursor(dictionary=True)
    try:
        # Serialize first-time claims on the parent file row so two clients
        # cannot both observe an empty lock slot and become writers.
        cursor.execute("SELECT file_id FROM files WHERE file_id=%s FOR UPDATE", (file_id,))
        if not cursor.fetchone():
            abort(404)
        cursor.execute(
            """SELECT l.user_id, l.lock_token, (l.expires_at > UTC_TIMESTAMP()) AS active,
                      u.username
               FROM file_edit_locks l JOIN users u ON u.user_id=l.user_id
               WHERE l.file_id=%s""",
            (file_id,),
        )
        current = cursor.fetchone()
        if current and current["active"] and int(current["user_id"]) != int(user_id):
            db().commit()
            return None, current["username"]

        token = current["lock_token"] if current and current["active"] else secrets.token_hex(32)
        cursor.execute(
            """INSERT INTO file_edit_locks (file_id,user_id,lock_token,expires_at)
               VALUES (%s,%s,%s,DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND))
               ON DUPLICATE KEY UPDATE user_id=VALUES(user_id), lock_token=VALUES(lock_token),
                 expires_at=VALUES(expires_at)""",
            (file_id, user_id, token, EDIT_LOCK_SECONDS),
        )
        db().commit()
        return token, None
    except Exception:
        db().rollback()
        raise
    finally:
        cursor.close()


def _active_edit_lock(file_id: int):
    cursor = db().cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT l.user_id,l.lock_token,u.username
               FROM file_edit_locks l JOIN users u ON u.user_id=l.user_id
               WHERE l.file_id=%s AND l.expires_at > UTC_TIMESTAMP()""",
            (file_id,),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def _assert_no_other_edit_lock(file_id: int, user_id: int):
    cursor = db().cursor()
    cursor.execute("SELECT file_id FROM files WHERE file_id=%s FOR UPDATE", (file_id,))
    if not cursor.fetchone():
        cursor.close()
        abort(404)
    cursor.close()
    lock = _active_edit_lock(file_id)
    if lock and int(lock["user_id"]) != int(user_id):
        abort(409, description=f"This file is being edited by {lock['username']}. Try again after they finish editing.")


def _require_edit_lock(file_id: int, user_id: int, token: str):
    cursor = db().cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT user_id,lock_token FROM file_edit_locks
               WHERE file_id=%s AND expires_at > UTC_TIMESTAMP() FOR UPDATE""",
            (file_id,),
        )
        lock = cursor.fetchone()
        if not token or not lock or int(lock["user_id"]) != int(user_id) or not secrets.compare_digest(lock["lock_token"], token):
            abort(409, description="Your edit lock expired or another user has the edit lock. Reload the file before editing.")
    finally:
        cursor.close()


def _store_upload(upload, suffix: str = "") -> Path:
    if upload is None or (hasattr(upload, "filename") and not upload.filename):
        raise ValueError("Choose a file first.")
    descriptor, filename = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=TEMP_ROOT)
    os.close(descriptor)
    path = Path(filename)
    try:
        if hasattr(upload, "save"):
            upload.save(path)
        else:
            with path.open("wb") as destination:
                shutil.copyfileobj(upload, destination)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _back_to_dashboard():
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//") and not urlparse(target).netloc:
        return redirect(target)
    return redirect(url_for("dashboard"))


@app.get("/healthz")
def healthz():
    return {"status": "ok", "application": "FileBox"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT user_id, username, email, password_hash FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        cursor.close()
        if not user or not verify_password(password, user["password_hash"]):
            flash("Email or password is incorrect.", "error")
            return render_template("login.html", mode="login", email=email)
        session.clear()
        session["user_id"] = user["user_id"]
        session.permanent = True
        _csrf_token()
        next_path = request.args.get("next", "")
        if next_path.startswith("/") and not next_path.startswith("//"):
            return redirect(next_path)
        return redirect(url_for("dashboard"))
    return render_template("login.html", mode="login", email="")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not username or len(username) > 80:
            flash("Enter a display name of 1 to 80 characters.", "error")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
            flash("Enter a valid email address.", "error")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "error")
        else:
            cursor = db().cursor()
            try:
                cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,%s)",
                               (username, email, hash_password(password)))
                user_id = cursor.lastrowid
                db().commit()
                (STORAGE_ROOT / str(user_id)).mkdir(parents=True, exist_ok=True)
                session.clear()
                session["user_id"] = user_id
                session.permanent = True
                _csrf_token()
                flash("Account created. You’re signed in.", "success")
                return redirect(url_for("dashboard"))
            except mysql.connector.IntegrityError:
                db().rollback()
                flash("That email is already registered. Sign in instead.", "error")
            finally:
                cursor.close()
    return render_template("login.html", mode="register", email=request.form.get("email", ""))


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


SORT_COLUMNS = {
    "name": "f.file_name",
    "type": "f.file_type",
    "size": "f.file_size",
    "created": "f.created_at",
    "modified": "f.modified_at",
}
FILE_TYPE_GROUPS = {
    "image": ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"],
    "audio": ["mp3", "wav", "ogg", "m4a", "flac", "aac"],
    "video": ["mp4", "webm", "mov", "mkv", "avi"],
    "document": ["pdf", "docx", "doc", "odt"],
    "spreadsheet": ["csv", "xlsx", "xls", "ods"],
    "text": ["txt", "md", "log", "json", "xml", "html", "htm", "css", "py", "js"],
}


def _accessible_files(section: str, user_id: int, folder_id=None, search="", category="",
                      sort="name", direction="asc"):
    query = """SELECT DISTINCT f.file_id, f.owner_id, f.folder_id, f.file_name, f.relative_path,
                     f.file_type, f.file_size, f.created_at, f.modified_at,
                     f.access_mode, f.allow_authenticated_download,
                     owner.username AS owner_name, p.permission_type AS user_permission,
                     p.allow_download AS user_download
              FROM files f JOIN users owner ON owner.user_id=f.owner_id
              LEFT JOIN permissions p ON p.file_id=f.file_id AND p.grantee_user_id=%s
              WHERE (f.owner_id=%s OR p.grantee_user_id=%s OR f.access_mode IN ('AUTH_READ','AUTH_WRITE'))"""
    params: list = [user_id, user_id, user_id]
    if section == "shared":
        query += " AND f.owner_id<>%s"
        params.append(user_id)
    elif section == "mine" and not search and not category:
        query += " AND f.owner_id=%s AND f.folder_id <=> %s"
        params.extend([user_id, folder_id])
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query += " AND f.file_name LIKE %s ESCAPE '\\\\'"
        params.append(f"%{escaped}%")
    if category:
        if category in FILE_TYPE_GROUPS:
            values = FILE_TYPE_GROUPS[category]
            query += " AND LOWER(f.file_type) IN (" + ",".join(["%s"] * len(values)) + ")"
            params.extend(values)
        else:
            extension = category.lower().lstrip(".")[:32]
            query += " AND LOWER(f.file_type)=%s"
            params.append(extension)
    column = SORT_COLUMNS.get(sort, SORT_COLUMNS["name"])
    direction_sql = "DESC" if direction == "desc" else "ASC"
    query += f" ORDER BY {column} {direction_sql}, f.file_id ASC LIMIT 300"
    cursor = db().cursor(dictionary=True)
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    for row in rows:
        owner = row["owner_id"] == user_id
        permission = row["user_permission"]
        mode = row["access_mode"]
        row["is_owner"] = owner
        row["can_read"] = owner or permission in {"READ", "WRITE"} or mode in {"AUTH_READ", "AUTH_WRITE"}
        row["can_write"] = owner or permission == "WRITE" or mode == "AUTH_WRITE"
        row["can_download"] = owner or (permission is not None and bool(row["user_download"])) or (
            mode in {"AUTH_READ", "AUTH_WRITE"} and bool(row["allow_authenticated_download"])
        )
        row["access_label"] = _access_label(row)
        row["size_label"] = _human_size(row["file_size"])
        row["modified_label"] = row["modified_at"].strftime("%Y-%m-%d %H:%M") if row["modified_at"] else ""
    return rows


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@app.get("/")
@app.get("/dashboard")
@login_required
def dashboard():
    user_id = g.current_user["user_id"]
    section = request.args.get("section", "mine")
    if section not in {"mine", "shared", "recent"}:
        section = "mine"
    search = request.args.get("q", "").strip()[:200]
    category = request.args.get("type", "").strip()[:32]
    default_sort = "modified" if section == "recent" else "name"
    sort = request.args.get("sort", default_sort)
    if sort not in SORT_COLUMNS:
        sort = "name"
    direction = "desc" if request.args.get("direction") == "desc" else "asc"
    folder_id = request.args.get("folder", "")
    current_folder = None
    parent_folder_id = None
    if section == "mine" and folder_id:
        if not folder_id.isdigit():
            abort(404)
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT * FROM folders WHERE folder_id=%s AND owner_id=%s", (int(folder_id), user_id))
        current_folder = cursor.fetchone()
        cursor.close()
        if not current_folder:
            abort(404)
        parent_folder_id = current_folder["folder_id"]

    files = _accessible_files(section, user_id, parent_folder_id, search, category, sort, direction)
    folders, breadcrumbs = [], []
    if section == "mine" and not search and not category:
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT folder_id, folder_name, parent_folder_id, relative_path FROM folders WHERE owner_id=%s AND parent_folder_id <=> %s ORDER BY folder_name",
                       (user_id, parent_folder_id))
        folders = cursor.fetchall()
        cursor.close()
        ancestor = current_folder
        while ancestor:
            breadcrumbs.insert(0, ancestor)
            if ancestor["parent_folder_id"] is None:
                break
            cursor = db().cursor(dictionary=True)
            cursor.execute("SELECT * FROM folders WHERE folder_id=%s AND owner_id=%s",
                           (ancestor["parent_folder_id"], user_id))
            ancestor = cursor.fetchone()
            cursor.close()
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT COALESCE(SUM(file_size),0) AS used_bytes, COUNT(*) AS file_count FROM files WHERE owner_id=%s", (user_id,))
    stats = cursor.fetchone()
    cursor.close()
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT folder_id,folder_name,relative_path FROM folders WHERE owner_id=%s ORDER BY relative_path", (user_id,))
    all_folders = cursor.fetchall()
    cursor.close()
    return render_template("dashboard.html", section=section, files=files, folders=folders,
                           breadcrumbs=breadcrumbs, current_folder=current_folder,
                           search=search, category=category, sort=sort, direction=direction,
                           stats=stats, all_folders=all_folders, human_size=_human_size)


@app.post("/folders")
@login_required
def create_folder():
    user_id = g.current_user["user_id"]
    name = safe_name(request.form.get("name", ""))
    parent_id = request.form.get("parent_folder_id", "") or None
    parent_path = ""
    if parent_id:
        if not str(parent_id).isdigit():
            abort(404)
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT relative_path FROM folders WHERE folder_id=%s AND owner_id=%s", (int(parent_id), user_id))
        parent = cursor.fetchone()
        cursor.close()
        if not parent:
            abort(404)
        parent_path = parent["relative_path"]
    relative = f"{parent_path}/{name}" if parent_path else name
    safe_storage_path(STORAGE_ROOT, user_id, relative, must_exist=False, expect_file=False)
    lock_key = f"folder:{user_id}:{relative.casefold()}"
    with SCHEDULER.acquire(lock_key, "write", priority=_priority(), user_id=user_id):
        directory = safe_storage_path(STORAGE_ROOT, user_id, relative, must_exist=False, expect_file=False)
        if directory.exists():
            flash("That folder name is already in use.", "error")
            return _back_to_dashboard()
        os_backend.make_directory(STORAGE_ROOT / str(user_id), relative)
        cursor = db().cursor()
        try:
            cursor.execute("INSERT INTO folders (owner_id,parent_folder_id,folder_name,relative_path) VALUES (%s,%s,%s,%s)",
                           (user_id, parent_id, name, relative))
            database.log_activity(db(), user_id, None, "FOLDER_CREATED", relative)
            db().commit()
        except Exception:
            db().rollback()
            try:
                os_backend.delete_empty_directory(STORAGE_ROOT / str(user_id), relative)
            except OSError:
                pass
            raise
        finally:
            cursor.close()
    flash(f"Folder ‘{name}’ created.", "success")
    return _back_to_dashboard()


@app.post("/upload")
@login_required
def upload_file():
    user_id = g.current_user["user_id"]
    parent_id = request.form.get("parent_folder_id", "") or None
    parent_path = ""
    if parent_id:
        if not str(parent_id).isdigit():
            abort(404)
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT relative_path FROM folders WHERE folder_id=%s AND owner_id=%s", (int(parent_id), user_id))
        parent = cursor.fetchone()
        cursor.close()
        if not parent:
            abort(404)
        parent_path = parent["relative_path"]
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Choose a file to upload.", "error")
        return _back_to_dashboard()
    name = safe_name(Path(uploaded.filename.replace("\\", "/")).name)
    relative = f"{parent_path}/{name}" if parent_path else name
    source = _store_upload(uploaded)
    root = STORAGE_ROOT / str(user_id)
    lock_key = f"file-path:{user_id}:{relative.casefold()}"
    try:
        with SCHEDULER.acquire(lock_key, "write", priority=_priority(), user_id=user_id):
            destination = safe_storage_path(STORAGE_ROOT, user_id, relative, must_exist=False)
            if destination.exists():
                flash("A file with that name already exists in this folder.", "error")
                return _back_to_dashboard()
            os_backend.import_file(root, str(source), relative)
            cursor = db().cursor()
            try:
                cursor.execute("INSERT INTO files (owner_id,folder_id,file_name,relative_path,file_type,file_size) VALUES (%s,%s,%s,%s,%s,%s)",
                               (user_id, parent_id, name, relative, Path(name).suffix.lower().lstrip(".")[:32], destination.stat().st_size))
                file_id = cursor.lastrowid
                database.log_activity(db(), user_id, file_id, "FILE_UPLOADED", name)
                db().commit()
            except Exception:
                db().rollback()
                try:
                    os_backend.delete_file(root, relative)
                except OSError:
                    pass
                raise
            finally:
                cursor.close()
        flash(f"‘{name}’ uploaded.", "success")
    finally:
        source.unlink(missing_ok=True)
    return _back_to_dashboard()


@app.post("/folders/<int:folder_id>/rename")
@login_required
def rename_folder(folder_id):
    user_id = g.current_user["user_id"]
    new_name = safe_name(request.form.get("name", ""))
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT * FROM folders WHERE folder_id=%s AND owner_id=%s", (folder_id, user_id))
    folder = cursor.fetchone()
    cursor.close()
    if not folder:
        abort(404)
    old = folder["relative_path"]
    parent = old.rsplit("/", 1)[0] if "/" in old else ""
    new = f"{parent}/{new_name}" if parent else new_name
    if old == new:
        return _back_to_dashboard()
    safe_storage_path(STORAGE_ROOT, user_id, new, must_exist=False, expect_file=False)
    with SCHEDULER.acquire(f"folder:{user_id}:{old.casefold()}", "write", priority=_priority(), user_id=user_id):
        root = STORAGE_ROOT / str(user_id)
        old_path = safe_storage_path(STORAGE_ROOT, user_id, old, expect_file=False)
        new_path = safe_storage_path(STORAGE_ROOT, user_id, new, must_exist=False, expect_file=False)
        if new_path.exists():
            flash("A folder with that name already exists beside it.", "error")
            return _back_to_dashboard()
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT folder_id, relative_path FROM folders WHERE owner_id=%s", (user_id,))
        descendant_folders = [row for row in cursor.fetchall()
                              if row["relative_path"] == old or row["relative_path"].startswith(old + "/")]
        cursor.execute("SELECT file_id, relative_path FROM files WHERE owner_id=%s", (user_id,))
        descendant_files = [row for row in cursor.fetchall() if row["relative_path"].startswith(old + "/")]
        cursor.close()
        os_backend.move_file(root, old, new)
        try:
            cursor = db().cursor()

            for child in descendant_folders:
                updated = new + child["relative_path"][len(old):]
                folder_name = new_name if child["folder_id"] == folder_id else child["relative_path"].rsplit("/", 1)[-1]
                cursor.execute("UPDATE folders SET relative_path=%s, folder_name=%s WHERE folder_id=%s AND owner_id=%s",
                               (updated, folder_name, child["folder_id"], user_id))
            for child in descendant_files:
                updated = new + child["relative_path"][len(old):]
                cursor.execute("UPDATE files SET relative_path=%s WHERE file_id=%s AND owner_id=%s",
                               (updated, child["file_id"], user_id))
            database.log_activity(db(), user_id, None, "FOLDER_RENAMED", f"{old} -> {new}")
            db().commit()
        except Exception:
            db().rollback()
            try:
                os_backend.move_file(root, new, old)
            except OSError:
                app.logger.exception("Could not roll back a folder rename")
            raise
        finally:
            cursor.close()
    flash(f"Folder renamed to ‘{new_name}’. ", "success")
    return _back_to_dashboard()


@app.post("/files/<int:file_id>/move")
@login_required
def move_file(file_id):
    info = _require_file(file_id, "write")
    if not info["is_owner"]:
        _deny(file_id, "RENAME_DENIED")
    name = safe_name(request.form.get("name", info["file_name"]))
    folder_id = request.form.get("folder_id", "") or None
    folder_path = ""
    if folder_id:
        if not str(folder_id).isdigit():
            abort(404)
        cursor = db().cursor(dictionary=True)
        cursor.execute("SELECT relative_path FROM folders WHERE folder_id=%s AND owner_id=%s", (int(folder_id), info["owner_id"]))
        folder = cursor.fetchone()
        cursor.close()
        if not folder:
            abort(404)
        folder_path = folder["relative_path"]
    new_relative = f"{folder_path}/{name}" if folder_path else name
    safe_storage_path(STORAGE_ROOT, info["owner_id"], new_relative, must_exist=False)
    old_relative = info["relative_path"]
    if new_relative == old_relative:
        return _back_to_dashboard()
    root = STORAGE_ROOT / str(info["owner_id"])
    with SCHEDULER.acquire(file_id, "write", priority=_priority(), user_id=g.current_user["user_id"], file_id=file_id):
        _assert_no_other_edit_lock(file_id, g.current_user["user_id"])
        new_path = safe_storage_path(STORAGE_ROOT, info["owner_id"], new_relative, must_exist=False)
        if new_path.exists():
            flash("A file with that name already exists in the destination folder.", "error")
            return _back_to_dashboard()
        os_backend.move_file(root, old_relative, new_relative)
        cursor = db().cursor()
        try:

            cursor.execute("UPDATE files SET file_name=%s,relative_path=%s,folder_id=%s,file_type=%s WHERE file_id=%s AND owner_id=%s",
                           (name, new_relative, folder_id, Path(name).suffix.lower().lstrip(".")[:32], file_id, info["owner_id"]))
            database.log_activity(db(), g.current_user["user_id"], file_id, "FILE_MOVED", f"{old_relative} -> {new_relative}")
            db().commit()
        except Exception:
            db().rollback()
            try:
                os_backend.move_file(root, new_relative, old_relative)
            except OSError:
                app.logger.exception("Could not roll back a file move")
            raise
        finally:
            cursor.close()
    flash(f"‘{name}’ moved.", "success")
    return _back_to_dashboard()


@app.post("/files/<int:file_id>/delete")
@login_required
def delete_file(file_id):
    info = _require_file(file_id, "write")
    if not info["is_owner"]:
        _deny(file_id, "DELETE_DENIED")
    relative = info["relative_path"]
    root = STORAGE_ROOT / str(info["owner_id"])
    staging_rel = f".filebox-staging/{secrets.token_hex(12)}"
    with SCHEDULER.acquire(file_id, "write", priority=_priority(), user_id=g.current_user["user_id"], file_id=file_id):
        _assert_no_other_edit_lock(file_id, g.current_user["user_id"])
        safe_storage_path(STORAGE_ROOT, info["owner_id"], relative)
        os_backend.make_directory(root, ".filebox-staging")
        os_backend.move_file(root, relative, staging_rel)
        cursor = db().cursor()
        try:

            database.log_activity(db(), g.current_user["user_id"], file_id, "FILE_DELETED", info["file_name"])
            cursor.execute("DELETE FROM files WHERE file_id=%s AND owner_id=%s", (file_id, info["owner_id"]))
            db().commit()
        except Exception:
            db().rollback()
            try:
                os_backend.move_file(root, staging_rel, relative)
            except OSError:
                app.logger.exception("Could not restore a file after delete rollback")
            raise
        finally:
            cursor.close()
        try:
            os_backend.delete_file(root, staging_rel)
        except OSError:
            app.logger.exception("Deleted file remains in the internal staging directory")
    flash(f"‘{info['file_name']}’ deleted.", "success")
    return _back_to_dashboard()


@app.post("/files/<int:file_id>/replace")
@login_required
def replace_file(file_id):
    info = _require_file(file_id, "write")
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose the replacement file.", "error")
        return redirect(url_for("preview_file", file_id=file_id))
    incoming_name = safe_name(Path(upload.filename.replace("\\", "/")).name)
    if Path(incoming_name).suffix.lower() != Path(info["file_name"]).suffix.lower():
        flash("The replacement file must have the same file extension.", "error")
        return redirect(url_for("preview_file", file_id=file_id))
    source = _store_upload(upload)
    root = STORAGE_ROOT / str(info["owner_id"])
    backup_rel = f".filebox-staging/{secrets.token_hex(12)}"
    with SCHEDULER.acquire(file_id, "write", priority=_priority(), user_id=g.current_user["user_id"], file_id=file_id):
        _assert_no_other_edit_lock(file_id, g.current_user["user_id"])
        try:
            latest = _require_file(file_id, "write")
            os_backend.make_directory(root, ".filebox-staging")
            os_backend.move_file(root, latest["relative_path"], backup_rel)
            try:
                os_backend.import_file(root, str(source), latest["relative_path"])
                new_size = safe_storage_path(STORAGE_ROOT, info["owner_id"], latest["relative_path"]).stat().st_size
                cursor = db().cursor()
                try:
        
                    cursor.execute("UPDATE files SET file_size=%s WHERE file_id=%s", (new_size, file_id))
                    database.log_activity(db(), g.current_user["user_id"], file_id, "FILE_REPLACED", latest["file_name"])
                    db().commit()
                except Exception:
                    db().rollback()
                    os_backend.delete_file(root, latest["relative_path"])
                    os_backend.move_file(root, backup_rel, latest["relative_path"])
                    raise
                finally:
                    cursor.close()
                os_backend.delete_file(root, backup_rel)
            except Exception:
                if safe_storage_path(STORAGE_ROOT, info["owner_id"], backup_rel, must_exist=False).exists():
                    try:
                        current = safe_storage_path(STORAGE_ROOT, info["owner_id"], latest["relative_path"], must_exist=False)
                        if current.exists():
                            os_backend.delete_file(root, latest["relative_path"])
                        os_backend.move_file(root, backup_rel, latest["relative_path"])
                    except OSError:
                        app.logger.exception("Could not restore the previous file version")
                raise
        finally:
            source.unlink(missing_ok=True)
    flash("File contents replaced.", "success")
    return redirect(url_for("preview_file", file_id=file_id))


EDITABLE_EXTENSIONS = {"txt", "md", "csv", "json", "xml", "html", "htm", "css", "js", "log"}


@app.post("/files/<int:file_id>/edit-lock/heartbeat")
@login_required
def edit_lock_heartbeat(file_id):
    _require_file(file_id, "write")
    token = request.form.get("edit_token", "")
    cursor = db().cursor()
    cursor.execute(
        """UPDATE file_edit_locks SET expires_at=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND)
           WHERE file_id=%s AND user_id=%s AND lock_token=%s AND expires_at > UTC_TIMESTAMP()""",
        (EDIT_LOCK_SECONDS, file_id, g.current_user["user_id"], token),
    )
    renewed = cursor.rowcount == 1
    cursor.close()
    if renewed:
        db().commit()
        return jsonify(ok=True, expires_in=EDIT_LOCK_SECONDS)
    db().rollback()
    return jsonify(ok=False, error="The edit lock expired or was taken by another user."), 409


@app.post("/files/<int:file_id>/edit-lock/release")
@login_required
def release_edit_lock(file_id):
    token = request.form.get("edit_token", "")
    cursor = db().cursor()
    cursor.execute(
        "DELETE FROM file_edit_locks WHERE file_id=%s AND user_id=%s AND lock_token=%s",
        (file_id, g.current_user["user_id"], token),
    )
    db().commit()
    cursor.close()
    return jsonify(released=True)


@app.post("/files/<int:file_id>/edit")
@login_required
def edit_text_file(file_id):
    info = _require_file(file_id, "write")
    extension = Path(info["file_name"]).suffix.lower().lstrip(".")
    if extension not in EDITABLE_EXTENSIONS:
        abort(415, description="This file type is not editable in the browser.")
    content = request.form.get("content", "")
    if len(content.encode("utf-8")) > MAX_PREVIEW_BYTES:
        abort(413, description="Text edits are limited to 4 MiB.")
    root = STORAGE_ROOT / str(info["owner_id"])
    relative = info["relative_path"]
    staged = f".filebox-staging/{secrets.token_hex(12)}"
    edit_token = request.form.get("edit_token", "")
    replacement = _store_upload(io.BytesIO(content.encode("utf-8")), suffix=".txt")
    with SCHEDULER.acquire(file_id, "write", priority=_priority(), user_id=g.current_user["user_id"], file_id=file_id):
        try:
            _require_edit_lock(file_id, g.current_user["user_id"], edit_token)
            os_backend.make_directory(root, ".filebox-staging")
            os_backend.import_file(root, str(replacement), staged)
            os_backend.move_file(root, relative, f"{staged}.old")
            try:
                os_backend.move_file(root, staged, relative)
                size = safe_storage_path(STORAGE_ROOT, info["owner_id"], relative).stat().st_size
                cursor = db().cursor()
                try:
        
                    cursor.execute("UPDATE files SET file_size=%s WHERE file_id=%s", (size, file_id))
                    database.log_activity(db(), g.current_user["user_id"], file_id, "FILE_EDITED", info["file_name"])
                    db().commit()
                except Exception:
                    db().rollback()
                    os_backend.delete_file(root, relative)
                    os_backend.move_file(root, f"{staged}.old", relative)
                    raise
                finally:
                    cursor.close()
                os_backend.delete_file(root, f"{staged}.old")
            except Exception:
                if safe_storage_path(STORAGE_ROOT, info["owner_id"], f"{staged}.old", must_exist=False).exists():
                    try:
                        current = safe_storage_path(STORAGE_ROOT, info["owner_id"], relative, must_exist=False)
                        if current.exists():
                            os_backend.delete_file(root, relative)
                        os_backend.move_file(root, f"{staged}.old", relative)
                    except OSError:
                        app.logger.exception("Could not restore the previous text file")
                raise
        finally:
            replacement.unlink(missing_ok=True)
            if safe_storage_path(STORAGE_ROOT, info["owner_id"], staged, must_exist=False).exists():
                try:
                    os_backend.delete_file(root, staged)
                except OSError:
                    pass
    flash("Text file saved.", "success")
    return redirect(url_for("preview_file", file_id=file_id))


@app.get("/files/<int:file_id>")
@login_required
def preview_file(file_id):
    info = _require_file(file_id, "read")
    extension = Path(info["file_name"]).suffix.lower().lstrip(".")
    mime = _mime_type(info["file_name"])
    preview_kind = "unsupported"
    preview_text = None
    csv_rows = None
    document_paragraphs = None
    spreadsheet_rows = None
    error_message = None
    if extension == "pdf":
        preview_kind = "pdf"
    elif extension in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}:
        preview_kind = "image"
    elif extension in {"mp3", "wav", "ogg", "m4a", "flac", "aac"}:
        preview_kind = "audio"
    elif extension in {"mp4", "webm", "mov", "mkv", "avi"}:
        preview_kind = "video"
    elif extension in {"html", "htm"}:
        preview_kind = "html"
    elif extension == "csv":
        preview_kind = "csv"
    elif extension == "docx":
        preview_kind = "docx"
    elif extension == "xlsx":
        preview_kind = "xlsx"
    elif extension in EDITABLE_EXTENSIONS:
        preview_kind = "text"
    if preview_kind in {"text", "csv", "docx", "xlsx"}:
        try:
            raw = _get_file_bytes(info, priority=_priority())
            if preview_kind == "text":
                preview_text = _decode_text_file(raw)
            elif preview_kind == "csv":
                decoded = raw.decode("utf-8-sig", errors="replace")
                csv_rows = list(csv.reader(io.StringIO(decoded)))[:300]
                csv_rows = [row[:30] for row in csv_rows]
            elif preview_kind == "docx":
                doc = Document(io.BytesIO(raw))
                document_paragraphs = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
                for table in doc.tables:
                    document_paragraphs.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows[:100])
            elif preview_kind == "xlsx":
                book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
                sheet = book[book.sheetnames[0]]
                spreadsheet_rows = [list(row[:30]) for row in sheet.iter_rows(min_row=1, max_row=300, values_only=True)]
                book.close()
        except Exception as exc:
            app.logger.exception("Preview failed for file ID %s", file_id)
            preview_kind = "unsupported"
            error_message = str(exc)[:250]
    edit_token = None
    edit_locked_by = None
    if preview_kind == "text" and info["can_write"]:
        edit_token, edit_locked_by = _claim_edit_lock(file_id, g.current_user["user_id"])
    _audit(g.current_user["user_id"], file_id, "FILE_OPENED", info["file_name"])
    return render_template("preview.html", info=info, extension=extension, mime=mime,
                           preview_kind=preview_kind, preview_text=preview_text,
                           csv_rows=csv_rows, document_paragraphs=document_paragraphs,
                           spreadsheet_rows=spreadsheet_rows, error_message=error_message,
                           editable=bool(edit_token), edit_token=edit_token,
                           edit_locked_by=edit_locked_by,
                           replace_allowed=info["can_write"] and not edit_locked_by)


@app.get("/files/<int:file_id>/content")
@login_required
def file_content(file_id):
    info = _require_file(file_id, "read")
    if request.args.get("download") == "1":
        if not info["can_download"]:
            _deny(file_id, "DOWNLOAD_DENIED")
        as_attachment = True
    else:
        as_attachment = False
    lease = SCHEDULER.acquire(file_id, "read", priority=_priority(),
                              user_id=g.current_user["user_id"], file_id=file_id)
    try:
        info = _require_file(file_id, "read")
        if as_attachment and not info["can_download"]:
            _deny(file_id, "DOWNLOAD_DENIED")
        path = safe_storage_path(STORAGE_ROOT, info["owner_id"], info["relative_path"])
        response = send_file(path, mimetype=_mime_type(info["file_name"]),
                             as_attachment=as_attachment, download_name=info["file_name"],
                             conditional=True, max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        extension = Path(info["file_name"]).suffix.lower()
        if extension in {".html", ".htm", ".svg"} and not as_attachment:
            response.headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src 'self' data: blob:; "
                "media-src 'self' data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
            )
        # Keep the read lease for the entire file stream. Wrapping the iterable
        # also guarantees release for WSGI servers and test clients that close
        # the response iterable without invoking Flask's on-close callbacks.
        original_iterable = response.response
        def locked_iterable():
            completed = False
            try:
                yield from original_iterable
                completed = True
            finally:
                close = getattr(original_iterable, "close", None)
                if close:
                    close()
                lease.release(success=completed)
        response.response = locked_iterable()
        response.call_on_close(lambda held=lease: held.release(success=True))
        return response
    except Exception:
        lease.release(success=False)
        raise


@app.get("/files/<int:file_id>/download")
@login_required
def download_file(file_id):
    info = _require_file(file_id, "read")
    if not info["can_download"]:
        _deny(file_id, "DOWNLOAD_DENIED")
    _audit(g.current_user["user_id"], file_id, "FILE_DOWNLOAD_REQUESTED", info["file_name"])
    return redirect(url_for("file_content", file_id=file_id, download=1))


@app.route("/files/<int:file_id>/share", methods=["GET", "POST"])
@login_required
def share_file(file_id):
    info = _require_file(file_id)
    if not info["is_owner"]:
        _deny(file_id, "SHARE_DENIED")
    results = []
    query = ""
    if request.method == "POST":
        query = request.form.get("user_query", "").strip()[:100]
        if query:
            cursor = db().cursor(dictionary=True)
            cursor.execute("SELECT user_id,username,email FROM users WHERE user_id<>%s AND (username LIKE %s OR email LIKE %s) ORDER BY username LIMIT 20",
                           (g.current_user["user_id"], f"%{query}%", f"%{query}%"))
            results = cursor.fetchall()
            cursor.close()
            if not results:
                flash("No matching FileBox users were found.", "info")
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT p.permission_id,p.grantee_user_id,p.permission_type,p.allow_download,p.created_at,u.username,u.email FROM permissions p JOIN users u ON u.user_id=p.grantee_user_id WHERE p.file_id=%s ORDER BY u.username",
                   (file_id,))
    grants = cursor.fetchall()
    cursor.close()
    return render_template("share.html", info=info, grants=grants, results=results, query=query)


@app.post("/files/<int:file_id>/share/user")
@login_required
def grant_file_access(file_id):
    info = _require_file(file_id)
    if not info["is_owner"]:
        _deny(file_id, "SHARE_DENIED")
    try:
        target_id = int(request.form.get("user_id", ""))
    except ValueError:
        abort(400)
    permission = request.form.get("permission", "READ").upper()
    if permission not in {"READ", "WRITE"}:
        abort(400)
    allow_download = 1 if request.form.get("allow_download") == "on" else 0
    if target_id == g.current_user["user_id"]:
        flash("You already own this file.", "info")
        return redirect(url_for("share_file", file_id=file_id))
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT user_id,username,email FROM users WHERE user_id=%s", (target_id,))
    target = cursor.fetchone()
    cursor.close()
    if not target:
        abort(404)
    cursor = db().cursor()
    try:
        cursor.execute("INSERT INTO permissions (file_id,grantee_user_id,permission_type,allow_download) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE permission_type=VALUES(permission_type),allow_download=VALUES(allow_download)",
                       (file_id, target_id, permission, allow_download))
        _audit(g.current_user["user_id"], file_id, "PERMISSION_CHANGED",
               f"{target['email']} assigned {permission}, download={'on' if allow_download else 'off'}")
    finally:
        cursor.close()
    flash(f"Shared with {target['username']} as {permission.lower()} access.", "success")
    return redirect(url_for("share_file", file_id=file_id))


@app.post("/files/<int:file_id>/share/revoke/<int:user_id>")
@login_required
def revoke_file_access(file_id, user_id):
    info = _require_file(file_id)
    if not info["is_owner"]:
        _deny(file_id, "SHARE_DENIED")
    cursor = db().cursor()
    cursor.execute("DELETE FROM permissions WHERE file_id=%s AND grantee_user_id=%s", (file_id, user_id))
    if cursor.rowcount:
        _audit(g.current_user["user_id"], file_id, "PERMISSION_REVOKED", f"user_id={user_id}")
    else:
        db().commit()
    cursor.close()
    flash("Access revoked.", "success")
    return redirect(url_for("share_file", file_id=file_id))


@app.post("/files/<int:file_id>/share/access-mode")
@login_required
def set_access_mode(file_id):
    info = _require_file(file_id)
    if not info["is_owner"]:
        _deny(file_id, "SHARE_DENIED")
    mode = request.form.get("access_mode", "PRIVATE")
    if mode not in {"PRIVATE", "AUTH_READ", "AUTH_WRITE"}:
        abort(400)
    allow_download = 1 if request.form.get("allow_download") == "on" else 0
    cursor = db().cursor()
    cursor.execute("UPDATE files SET access_mode=%s,allow_authenticated_download=%s WHERE file_id=%s AND owner_id=%s",
                   (mode, allow_download, file_id, g.current_user["user_id"]))
    _audit(g.current_user["user_id"], file_id, "FILE_ACCESS_MODE_CHANGED", mode)
    cursor.close()
    flash("File access settings saved.", "success")
    return redirect(url_for("share_file", file_id=file_id))


@app.get("/activity")
@login_required
def activity():
    cursor = db().cursor(dictionary=True)
    cursor.execute("SELECT action,details,created_at FROM activity_log WHERE user_id=%s ORDER BY created_at DESC LIMIT 200",
                   (g.current_user["user_id"],))
    entries = cursor.fetchall()
    cursor.close()
    return render_template("activity.html", entries=entries)


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You do not have permission to access that item."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="That FileBox item could not be found."), 404


@app.errorhandler(413)
def upload_too_large(error):
    return render_template("error.html", code=413, message="The upload is larger than the configured FileBox limit."), 413


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "Invalid request.")), 400


@app.errorhandler(ValueError)
def invalid_value(error):
    return render_template("error.html", code=400, message=str(error)), 400


@app.errorhandler(mysql.connector.Error)
def database_error(error):
    app.logger.exception("MySQL request failed")
    return render_template("error.html", code=503,
                           message="FileBox could not reach MySQL. Check that MySQL is running and .env has the correct database settings."), 503


@app.errorhandler(Exception)
def unexpected_error(error):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("Unexpected FileBox web request failure")
    return render_template("error.html", code=500,
                           message="FileBox could not complete this operation. No absolute storage paths are shown here."), 500


def main():
    host = os.environ.get("FILEBOX_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("FILEBOX_WEB_PORT", "80"))
    url = f"http://{host}:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    serve(app, host=host, port=port, threads=max(4, int(os.environ.get("FILEBOX_WEB_THREADS", "8"))))


if __name__ == "__main__":
    main()
