"""密碼雜湊、初始管理員帳號、session 管理（見 SPEC.md 第 4 節）。"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
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

# icacls 逾時（秒）：只是收緊 ACL，卡住不值得擋住首次啟動。
_ICACLS_TIMEOUT = 10


def _restrict_file_permissions(path: Path) -> None:
    """盡力把密碼檔權限收到「只有目前使用者可讀」。

    POSIX 走 ``os.chmod(0o600)``。Windows 上 ``os.chmod`` 只能切唯讀旗標、對 ACL
    是 no-op，等於毫無保護，因此改用 ``icacls`` 砍掉繼承並只留目前使用者唯讀。
    這是 best-effort：icacls 不存在、逾時、或回非零（權限不足、非 NTFS 磁碟區、
    網路磁碟機）都只記警告，不中斷首次啟動流程——否則會變成「權限收不緊就完全
    無法開機」，比留一個權限較寬的檔案更糟。
    """
    if sys.platform == "win32":
        username = os.environ.get("USERNAME", "")
        if not username:
            logger.warning("無法取得 USERNAME 環境變數，略過 %s 的 ACL 收緊", path.name)
            return
        try:
            proc = subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{username}:R",
                ],
                capture_output=True,
                text=True,
                timeout=_ICACLS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("收緊 %s 權限失敗（icacls 無法執行）：%s", path.name, exc)
            return
        if proc.returncode != 0:
            logger.warning(
                "收緊 %s 權限失敗（icacls 回傳 %s）：%s",
                path.name,
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip(),
            )
        return

    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("收緊 %s 權限失敗：%s", path.name, exc)


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
    _restrict_file_permissions(admin_file)
    logger.info("已建立初始管理員帳號 admin，密碼檔：%s", admin_file)
    if sys.platform == "win32":
        logger.warning(
            "Windows 上此檔無完整權限保護，讀完立即刪除：%s", admin_file
        )


def new_session(conn: sqlite3.Connection, username: str, hours: float) -> str:
    """建立新 session，回傳 token（secrets.token_urlsafe(32)）。"""
    token = secrets.token_urlsafe(32)
    db.create_session(conn, token, username, hours)
    return token


def check_session(conn: sqlite3.Connection, token: str) -> str | None:
    """回傳 session 對應的 username；不存在或已過期則回 None。"""
    row = db.get_session(conn, token)
    return row["username"] if row is not None else None
