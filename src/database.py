"""MySQL connection and schema setup for FileBox."""
from __future__ import annotations

import os
import re
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def connect():
    return mysql.connector.connect(
        host=os.getenv("FILEBOX_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("FILEBOX_DB_PORT", "3306")),
        user=os.getenv("FILEBOX_DB_USER", "root"),
        password=os.getenv("FILEBOX_DB_PASSWORD", ""),
        database=os.getenv("FILEBOX_DB_NAME", "filebox"),
        autocommit=False,
        connection_timeout=5,
    )


def initialize():
    """Create the configured database and tables on first use.

    The MySQL account in .env must have permission to create databases. An
    administrator can also run schema.sql manually and grant normal access.
    """
    name = os.getenv("FILEBOX_DB_NAME", "filebox")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("FILEBOX_DB_NAME may contain only letters, digits, and underscores.")
    server = mysql.connector.connect(
        host=os.getenv("FILEBOX_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("FILEBOX_DB_PORT", "3306")),
        user=os.getenv("FILEBOX_DB_USER", "root"),
        password=os.getenv("FILEBOX_DB_PASSWORD", ""),
        autocommit=True,
        connection_timeout=5,
    )
    try:
        cursor = server.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cursor.close()
    finally:
        server.close()
    ensure_schema()


def ensure_schema():
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(80) NOT NULL,
            email VARCHAR(254) NOT NULL UNIQUE,
            password_hash VARCHAR(256) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS folders (
            folder_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            owner_id BIGINT NOT NULL,
            parent_folder_id BIGINT NULL,
            folder_name VARCHAR(255) NOT NULL,
            relative_path VARCHAR(1024) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_folder_path (owner_id, relative_path(190)),
            CONSTRAINT fk_folders_owner FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
            CONSTRAINT fk_folders_parent FOREIGN KEY (parent_folder_id) REFERENCES folders(folder_id) ON DELETE SET NULL
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS files (
            file_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            owner_id BIGINT NOT NULL,
            folder_id BIGINT NULL,
            file_name VARCHAR(255) NOT NULL,
            relative_path VARCHAR(1024) NOT NULL,
            file_type VARCHAR(32) NOT NULL DEFAULT '',
            file_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
            access_mode ENUM('PRIVATE','AUTH_READ','AUTH_WRITE') NOT NULL DEFAULT 'PRIVATE',
            allow_authenticated_download TINYINT(1) NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            modified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            CONSTRAINT fk_files_owner FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
            CONSTRAINT fk_files_folder FOREIGN KEY (folder_id) REFERENCES folders(folder_id) ON DELETE SET NULL,
            UNIQUE KEY uq_file_owner_path (owner_id, relative_path(190)),
            INDEX idx_files_name (file_name), INDEX idx_files_owner (owner_id)
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS permissions (
            permission_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            file_id BIGINT NOT NULL,
            grantee_user_id BIGINT NOT NULL,
            permission_type ENUM('READ','WRITE') NOT NULL DEFAULT 'READ',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_file_grantee (file_id, grantee_user_id),
            CONSTRAINT fk_permissions_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
            CONSTRAINT fk_permissions_user FOREIGN KEY (grantee_user_id) REFERENCES users(user_id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS activity_log (
            log_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            user_id BIGINT NULL,
            file_id BIGINT NULL,
            action VARCHAR(40) NOT NULL,
            details VARCHAR(500) NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_activity_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL,
            CONSTRAINT fk_activity_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE SET NULL,
            INDEX idx_activity_user_time (user_id, created_at)
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS file_edit_locks (
            file_id BIGINT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            lock_token CHAR(64) NOT NULL,
            expires_at DATETIME NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            CONSTRAINT fk_edit_locks_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
            CONSTRAINT fk_edit_locks_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            INDEX idx_edit_locks_expiry (expires_at)
        ) ENGINE=InnoDB""",
    ]
    db = connect()
    try:
        cursor = db.cursor()
        for statement in statements:
            cursor.execute(statement)
        _ensure_column(cursor, "files", "access_mode",
                       "ENUM('PRIVATE','AUTH_READ','AUTH_WRITE') NOT NULL DEFAULT 'PRIVATE'")
        _ensure_column(cursor, "permissions", "allow_download", "TINYINT(1) NOT NULL DEFAULT 1")
        _ensure_column(cursor, "files", "allow_authenticated_download", "TINYINT(1) NOT NULL DEFAULT 1")
        _ensure_index(cursor, "files", "idx_files_owner_created", "owner_id, created_at")
        _ensure_index(cursor, "files", "idx_files_owner_modified", "owner_id, modified_at")
        _ensure_index(cursor, "files", "idx_files_owner_type", "owner_id, file_type")
        _ensure_index(cursor, "permissions", "idx_permissions_grantee", "grantee_user_id, file_id")
        cursor.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='users' AND COLUMN_NAME='email'"
        )
        email_length = cursor.fetchone()
        if email_length and (email_length[0] or 0) < 254:
            cursor.execute("ALTER TABLE users MODIFY COLUMN email VARCHAR(254) NOT NULL")
        db.commit()
        cursor.close()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_column(cursor, table: str, column: str, definition: str):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (table, column),
    )
    if not cursor.fetchone()[0]:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def _ensure_index(cursor, table: str, index: str, columns: str):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (table, index),
    )
    if not cursor.fetchone()[0]:
        cursor.execute(f"ALTER TABLE `{table}` ADD INDEX `{index}` ({columns})")


def log_activity(db, user_id: int | None, file_id: int | None, action: str, details: str = ""):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO activity_log (user_id, file_id, action, details) VALUES (%s, %s, %s, %s)",
        (user_id, file_id, action, details[:500]),
    )
    cursor.close()
