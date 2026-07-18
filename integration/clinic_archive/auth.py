"""密碼雜湊、初始管理員帳號、session 管理（見 SPEC.md 第 4 節）。"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from . import db

logger = logging.getLogger(__name__)

# scrypt 參數：n=2**14 r=8 p=1；格式 scrypt$<salt_hex>$<hash_hex>。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_SALT_BYTES = 16
_SCRYPT_SCHEME = "scrypt"

FIRST_RUN_ADMIN_FILENAME = "FIRST_RUN_ADMIN.txt"


def hash_pw(pw: str) -> str:
    """回傳 `scrypt$<salt_hex>$<hash_hex>` 格式的雜湊字串。"""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        pw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"{_SCRYPT_SCHEME}${salt.hex()}${digest.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    """核對明碼密碼與已儲存的雜湊字串；格式不符一律回 False，不拋例外。"""
    parts = stored.split("$")
    if len(parts) != 3:
        return False
    scheme, salt_hex, hash_hex = parts
    if scheme != _SCRYPT_SCHEME:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    candidate = hashlib.scrypt(
        pw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected)
    )
    return secrets.compare_digest(candidate, expected)


def ensure_initial_admin(conn: sqlite3.Connection, data_root: str | Path) -> None:
    """users 表為空才建立初始 admin；已有任何使用者則什麼都不做（只建一次）。"""
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
        return

    password = secrets.token_urlsafe(9)
    pwhash = hash_pw(password)
    db.create_user(conn, "admin", pwhash, "manager")

    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    admin_file = root / FIRST_RUN_ADMIN_FILENAME
    admin_file.write_text(
        "首次啟動已自動建立管理員帳號，請盡快登入系統改密碼，並刪除本檔案。\n"
        "帳號：admin\n"
        f"密碼：{password}\n",
        encoding="utf-8",
    )
    os.chmod(admin_file, 0o600)
    logger.info("已建立初始管理員帳號 admin，密碼檔：%s", admin_file)


def new_session(conn: sqlite3.Connection, username: str, hours: float) -> str:
    """建立新 session，回傳 token（secrets.token_urlsafe(32)）。"""
    token = secrets.token_urlsafe(32)
    db.create_session(conn, token, username, hours)
    return token


def check_session(conn: sqlite3.Connection, token: str) -> str | None:
    """回傳 session 對應的 username；不存在或已過期則回 None。"""
    row = db.get_session(conn, token)
    return row["username"] if row is not None else None
