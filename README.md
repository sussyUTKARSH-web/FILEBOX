# FileBox

FileBox is an educational local file manager that connects three layers:

- **Python / Tkinter:** desktop interface, authentication screens, search, sharing, and activity view.
- **C file-system backend:** creates directories and copies, moves, exports, and deletes the actual files. Python sends requests to a compiled C helper executable, so the helper and Python can use different architectures.
- **MySQL:** stores users, folder and file metadata, read/write shares, and activity records. File contents stay in the local `storage/` directory.

## Features

- Register and log in with email and a salted PBKDF2 password hash.
- Upload, download, replace, rename, move, and delete files.
- Create nested folders and browse by folder.
- Search files by name and sort the visible list by name, type, size, or modified time.
- Share files with another FileBox account as `READ` or `WRITE`.
- View a user's recent activity.
- Keep actual files in `storage/<user_id>/`; keep searchable metadata and access records in MySQL.

## Setup (Windows)

Prerequisites: Python 3.10+, MySQL Server, and GCC (MinGW-w64).

1. Start MySQL Server. On first login or registration, FileBox creates the configured database and its five tables (`users`, `folders`, `files`, `permissions`, and `activity_log`) automatically. The MySQL account in `.env` needs permission to create databases. Otherwise, have an administrator run `schema.sql` and grant that account access.
2. Run `setup.ps1` from PowerShell. It creates `.venv`, installs the Python packages, and builds `c_backend/filebox_os.exe`.
3. Copy `.env.example` to `.env` if the launcher has not already done so. Set `FILEBOX_DB_USER` and `FILEBOX_DB_PASSWORD` to a MySQL account that can use the `filebox` database.
4. Run `run.ps1`.

The C helper is rebuilt on launch when its source file is newer than the executable. Keep GCC on `PATH` for that automatic rebuild.

## Team module split

1. **OS module A — File and folder operations:** C backend, local storage paths, file metadata handoff.
2. **OS module B — Synchronization and request order:** Python's OS adapter places requests in a FIFO worker queue (FCFS) and uses a binary semaphore around file mutations.
3. **DBMS module A — Users, folders, files, and search:** schema, keys, constraints, CRUD, filters, and sorting queries.
4. **DBMS module B — Sharing and logs:** permission records, read/write checks, joins, and activity history.

## Current scope notes

- Storage is local to this computer; this is not cloud storage.
- The database stores paths and metadata, not file bytes.
- `WRITE` permission allows replacing file contents. Only the owner can rename, move, share, or delete a file.
- Folder deletion is not exposed in the interface; files can be removed individually.
- MySQL and Python setup are external prerequisites. Do not put database passwords in source files; use `.env`.
