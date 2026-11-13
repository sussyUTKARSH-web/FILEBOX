CREATE DATABASE IF NOT EXISTS filebox
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE filebox;

CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(80) NOT NULL,
  email VARCHAR(254) NOT NULL UNIQUE,
  password_hash VARCHAR(256) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS folders (
  folder_id BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_id BIGINT NOT NULL,
  parent_folder_id BIGINT NULL,
  folder_name VARCHAR(255) NOT NULL,
  relative_path VARCHAR(1024) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_folder_path (owner_id, relative_path(190)),
  CONSTRAINT fk_folders_owner FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
  CONSTRAINT fk_folders_parent FOREIGN KEY (parent_folder_id) REFERENCES folders(folder_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS files (
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
  UNIQUE KEY uq_file_owner_path (owner_id, relative_path(190)),
  INDEX idx_files_name (file_name),
  INDEX idx_files_owner (owner_id),
  INDEX idx_files_owner_created (owner_id, created_at),
  INDEX idx_files_owner_modified (owner_id, modified_at),
  INDEX idx_files_owner_type (owner_id, file_type),
  CONSTRAINT fk_files_owner FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
  CONSTRAINT fk_files_folder FOREIGN KEY (folder_id) REFERENCES folders(folder_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS permissions (
  permission_id BIGINT PRIMARY KEY AUTO_INCREMENT,
  file_id BIGINT NOT NULL,
  grantee_user_id BIGINT NOT NULL,
  permission_type ENUM('READ','WRITE') NOT NULL DEFAULT 'READ',
  allow_download TINYINT(1) NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_file_grantee (file_id, grantee_user_id),
  INDEX idx_permissions_grantee (grantee_user_id, file_id),
  CONSTRAINT fk_permissions_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
  CONSTRAINT fk_permissions_user FOREIGN KEY (grantee_user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB;


CREATE TABLE IF NOT EXISTS activity_log (
  log_id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NULL,
  file_id BIGINT NULL,
  action VARCHAR(40) NOT NULL,
  details VARCHAR(500) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_activity_user_time (user_id, created_at),
  CONSTRAINT fk_activity_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL,
  CONSTRAINT fk_activity_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS file_edit_locks (
  file_id BIGINT PRIMARY KEY,
  user_id BIGINT NOT NULL,
  lock_token CHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_edit_locks_expiry (expires_at),
  CONSTRAINT fk_edit_locks_file FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
  CONSTRAINT fk_edit_locks_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB;
