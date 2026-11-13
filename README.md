# FileBox

FileBox is a local, multi-user file manager with a browser interface. Python serves the UI and enforces authorization, MySQL stores users and metadata, and the existing C helper performs managed file-system operations. File bytes remain under `storage/<user_id>/`; browser routes never accept or expose absolute storage paths.

## Existing project layers

- `src/web_app.py` is the browser application. Waitress serves it locally on `127.0.0.1:80` by default.
- `src/app.py` remains the optional Tkinter desktop client (`run_desktop.ps1`).
- `src/database.py` owns MySQL connections, schema creation, and backward-compatible additive migrations.
- `src/os_backend.py` keeps the FCFS file-operation request queue and dispatches independent operations to a bounded worker pool.
- `src/access_scheduler.py` maintains per-file reader/writer state, priority aging, and FIFO tie breaking.
- `c_backend/filebox_os.c` performs copying, moving, deleting, folder creation, size reads, and relative path validation.
- `schema.sql` is the complete fresh-install schema; `storage/` contains file bytes, not MySQL.

## Browser features

- Registration, login, and logout using the existing salted PBKDF2 password hash format.
- My Files, Shared With Me, Recent Files, folder navigation, name search, type filters, and validated sorting.
- Upload, folder creation, folder rename, file rename/move, replacement, text editing, download, and deletion.
- Individual READ/WRITE shares, revocation, download controls, and authenticated-user access modes (Private, Open Read, Open Write).
- Authenticated PDF, image, text, CSV, HTML, audio, video, DOCX, and XLSX previews. HTML is served in a sandboxed iframe with a restrictive CSP. Unsupported formats show file details and a download option when allowed.
- Activity records for file operations, shares, denials, and reader/writer queue events.

## Reader/writer and request scheduling

Every browser file operation checks access in Python before touching storage. A per-file condition protects reads and writes: multiple readers may hold a file together, while a writer requires exclusive access. A per-file semaphore guards the writer slot. A request queue is ordered by effective priority (Low=0, Normal=1, High=2), then arrival sequence. Every configured aging interval increases a waiter's effective priority until it reaches High, so a stream of newer high-priority requests cannot starve an older request. Readers may join the active read group only while they precede the first waiting writer in queue order. Operations on different file IDs use different lock state.

The FCFS OS worker queue admits C file-operation jobs in arrival order; a bounded worker pool executes jobs for unrelated paths concurrently. The access scheduler, rather than the pool, resolves conflicting work on each file. Request, acquisition, wait duration, release, and success/failure events are recorded in `activity_log`.

Priority is chosen per request in the viewer controls. The owner receives no automatic priority. Equal effective priorities use FIFO order.

## Windows setup

Prerequisites: Python 3.10+, MySQL Server, and GCC/MinGW-w64.

1. In PowerShell, open the project folder `C:\PROJECT-DBMS\FILEBOX`.
2. Run `Set-ExecutionPolicy -Scope Process Bypass` and then `.\setup.ps1`. Setup creates `.venv`, installs Flask, Waitress, MySQL Connector, Pillow, python-docx, and openpyxl, and compiles the C helper.
3. Check `.env`: set `FILEBOX_DB_HOST`, `FILEBOX_DB_PORT`, `FILEBOX_DB_NAME`, `FILEBOX_DB_USER`, and `FILEBOX_DB_PASSWORD` for a MySQL account that can create/use the database. Set `FILEBOX_SECRET_KEY` to a long random secret before exposing the app. The web app performs additive migrations on first request; existing rows are preserved.
4. Start MySQL.
5. Start FileBox in PowerShell with `.\run.ps1`. It serves `http://127.0.0.1:80` and opens the browser. Use `FILEBOX_WEB_PORT` in `.env` to change the local port.
6. Create a test user from the registration page. For permissions demos, register a second test user with a different email; do not use real passwords for evaluation accounts.

The Tkinter client is still available separately with `.\run_desktop.ps1`.

## Cloudflare Tunnel (temporary demo URL)

`tools/cloudflared.exe` is the official 64-bit Windows client downloaded from Cloudflare's release link. `run_tunnel.ps1` forwards to the local origin `http://127.0.0.1:80` (80:80). Start `run.ps1` first, then run `.\run_tunnel.ps1` in a second PowerShell window. Keep both running and copy the `trycloudflare.com` URL printed by cloudflared. A Quick Tunnel URL is temporary and does not create a named tunnel or custom domain. Stop it with Ctrl+C. Do not expose FileBox without setting `FILEBOX_SECRET_KEY` and using test accounts.

For a permanent hostname, create/configure a named Cloudflare Tunnel and DNS route in your Cloudflare account; that requires a domain on the account. The local origin remains `http://127.0.0.1:80`.

## Evaluation sequence

1. Register User A and upload a PDF, image, text file, CSV, DOCX, or XLSX. Open a preview and download a copy.
2. Create folders, upload files into one, rename a folder, and move/rename a file.
3. Register User B in a second browser profile or private window. Share one file as READ and disable downloads; verify B can preview but not download/edit. Share another as WRITE; verify B can replace or edit supported text while delete/share controls remain unavailable.
4. Revoke B and verify subsequent preview/download requests are denied. Try a private file while signed into B.
5. Change a file to Open Read, then Open Write, and verify the rights with a signed-in second account. Anonymous requests redirect to login.
6. For synchronization demonstrations, submit concurrent requests to the same file using Low/Normal/High priorities; check Activity for queued/acquired/released events and wait times. The scheduler also has repeatable automated tests in `tests/test_access_scheduler.py`.

## Database changes

Existing `users`, `folders`, `files`, `permissions`, and `activity_log` tables are retained. On initialization, FileBox adds `files.access_mode`, `files.allow_authenticated_download`, `permissions.allow_download`, and supporting indexes if missing, and widens legacy user email columns to 254 characters. New files default to `PRIVATE`; individual grants remain READ or WRITE. MySQL DDL migrations are additive and idempotent. `schema.sql` matches the current schema for new databases.

## Limitations

- This is a local educational deployment, not a hardened production cloud storage service. The Cloudflare Quick Tunnel URL is public while active; keep accounts and test files non-sensitive.
- File bytes remain local to the Windows machine. A tunnel does not replicate or back up storage.
- Browser text previews/edits are limited to 4 MiB. CSV, DOCX, and XLSX previews are bounded to 300 rows and 30 columns where applicable. Complex DOCX/XLSX styling is not reproduced.
- Older Tkinter actions predate the browser permission and scheduling model; use the web app for multi-user sharing and concurrency demonstrations.
- Cross-resource MySQL/filesystem operations use compensating rollback for ordinary failures, but a sudden power loss between the filesystem step and database commit may need manual recovery from `.filebox-staging`.
