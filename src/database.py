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
    ]
    db = connect()
    try:
        cursor = db.cursor()
        for statement in statements:
            cursor.execute(statement)
        db.commit()
        cursor.close()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def log_activity(db, user_id: int, file_id: int | None, action: str, details: str = ""):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO activity_log (user_id, file_id, action, details) VALUES (%s, %s, %s, %s)",
        (user_id, file_id, action, details[:500]),
    )
    cursor.close()
