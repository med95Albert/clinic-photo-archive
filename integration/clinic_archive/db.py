"""SQLite 存取層：連線／schema／DAO。

`connect()` 只負責開連線＋設定 pragma；`init_db()` 執行 DDL（idempotent，
可安全重複呼叫）。所有 DAO 一律 `(conn, …)` 簽名、一律參數化查詢，不做字串
拼接 SQL。DDL 內容與 SPEC.md 第 3 節逐字一致。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

# 與 SPEC.md 第 3 節逐字一致。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS patients(
  patient_key TEXT PRIMARY KEY,        -- 身分證號 或 P-0000123
  name TEXT, dob TEXT,                 -- dob ISO YYYY-MM-DD，可 NULL
  chart_no TEXT,                       -- HIS 病歷號別名，可 NULL
  created_by TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_key TEXT NOT NULL REFERENCES patients(patient_key),
  taken_date TEXT NOT NULL,            -- YYYY-MM-DD
  rtype TEXT NOT NULL,                 -- 病灶照|檢驗|InBody|文件|其他
  subtype TEXT,                        -- CBC|生化|尿液|過敏原|…
  src TEXT NOT NULL,                   -- phone|inbox|import
  path TEXT NOT NULL, sha256 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'auto', -- auto|confirmed
  batch_key TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS processed_batches(
  batch_key TEXT PRIMARY KEY,          -- "{pid|~}|{YYYY-MM-DD}|{HHMMSS}"
  state TEXT NOT NULL,                 -- auto_filed|queued
  processed_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS queue_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                  -- photo_batch|report|straggler|card_suspect
  reason TEXT NOT NULL,
  payload TEXT NOT NULL,               -- JSON：{files:[…], extracted:{…}, batch_key}
  state TEXT NOT NULL DEFAULT 'open',  -- open|resolved
  resolution TEXT, resolved_by TEXT, resolved_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pwhash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','manager')),
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
  expires_at TEXT NOT NULL,
  csrf TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  actor TEXT NOT NULL,                 -- 帳號 或 'system'
  action TEXT NOT NULL,                -- login|view_timeline|view_file|auto_file|queue|resolve|merge|reassign|create_user|delete_card
  patient_key TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_records_patient ON records(patient_key, taken_date);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """開一條 sqlite3 連線：WAL、foreign_keys=ON、Row factory。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # 併發寫入時（watcher 執行緒 + web 請求）遇到 SQLITE_BUSY 先自旋等待，
    # 而非立刻拋 OperationalError；10 秒足夠涵蓋單機的短暫寫鎖競爭。
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """執行 DDL；CREATE TABLE/INDEX 皆 IF NOT EXISTS，可安全重複呼叫。"""
    conn.executescript(SCHEMA_SQL)
    # 舊庫（round-1 schema）沒有 sessions.csrf 欄：CREATE IF NOT EXISTS 不會補欄，
    # 這裡以 PRAGMA 檢查後 ALTER 補上，讓升級後的既有資料庫仍能登入（審查 R2 P1）。
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "csrf" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN csrf TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# patients
# ---------------------------------------------------------------------------


def upsert_patient(
    conn: sqlite3.Connection,
    patient_key: str,
    name: str | None = None,
    dob: str | None = None,
    chart_no: str | None = None,
    created_by: str | None = None,
) -> None:
    """新建或更新病人。已存在時只覆蓋本次有給值的欄位，不會用 NULL 蓋掉舊值。"""
    conn.execute(
        """
        INSERT INTO patients(patient_key, name, dob, chart_no, created_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(patient_key) DO UPDATE SET
            name = COALESCE(excluded.name, patients.name),
            dob = COALESCE(excluded.dob, patients.dob),
            chart_no = COALESCE(excluded.chart_no, patients.chart_no)
        """,
        (patient_key, name, dob, chart_no, created_by),
    )
    conn.commit()


def insert_patient_strict(
    conn: sqlite3.Connection,
    patient_key: str,
    name: str | None,
    dob: str | None,
    chart_no: str | None,
    created_by: str | None,
) -> None:
    """純 INSERT 新建病人；patient_key 已存在時放行 ``sqlite3.IntegrityError`` 往上拋。

    與 ``upsert_patient`` 的差異：upsert 遇既有鍵會靜默更新，適合系統回填；
    本函式用於「人工建新檔」情境（Fix-B 佇列裁決建檔），要求呼叫端明確面對
    「這個鍵已經有人了」的衝突，不得靜默覆寫既有病人資料。
    """
    conn.execute(
        "INSERT INTO patients(patient_key, name, dob, chart_no, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (patient_key, name, dob, chart_no, created_by),
    )
    conn.commit()


def get_patient(conn: sqlite3.Connection, patient_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM patients WHERE patient_key = ?", (patient_key,)
    ).fetchone()


def find_patients_by_chart(conn: sqlite3.Connection, chart_no: str) -> list[sqlite3.Row]:
    """回傳所有 chart_no 命中的病人列（可能 0、1 或多筆）。

    病歷號在 patients 表無唯一約束（HIS 別名可能重複），故不得用 LIMIT 1
    盲取第一筆——那會把「同號多人」靜默配給任一人。呼叫端（predicate）必須
    自行檢查 ``len==1`` 才可據以歸檔，其餘一律 fail-closed 進佇列。
    """
    return conn.execute(
        "SELECT * FROM patients WHERE chart_no = ? ORDER BY patient_key", (chart_no,)
    ).fetchall()


def find_patient_by_chart(conn: sqlite3.Connection, chart_no: str) -> sqlite3.Row | None:
    """相容包裝：回第一筆命中或 None（不供歸檔判準用）。

    保留給只需「隨便找一筆」的既有呼叫點（如 UI 顯示）；歸檔判準一律改用
    ``find_patients_by_chart`` 並檢查唯一性，避免重號誤配。
    """
    rows = find_patients_by_chart(conn, chart_no)
    return rows[0] if rows else None


def list_patients(conn: sqlite3.Connection, search: str | None = None) -> list[sqlite3.Row]:
    """病人搜尋：search 為 None 時列出全部，否則對 key/name/chart_no 做 LIKE。"""
    if search:
        like = f"%{search}%"
        return conn.execute(
            """
            SELECT * FROM patients
            WHERE patient_key LIKE ? OR name LIKE ? OR chart_no LIKE ?
            ORDER BY patient_key
            """,
            (like, like, like),
        ).fetchall()
    return conn.execute("SELECT * FROM patients ORDER BY patient_key").fetchall()


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def insert_record(
    conn: sqlite3.Connection,
    patient_key: str,
    taken_date: str,
    rtype: str,
    subtype: str | None,
    src: str,
    path: str,
    sha256: str,
    status: str = "auto",
    batch_key: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO records(patient_key, taken_date, rtype, subtype, src, path, sha256, status, batch_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (patient_key, taken_date, rtype, subtype, src, path, sha256, status, batch_key),
    )
    conn.commit()
    return int(cur.lastrowid)


def records_for_patient(conn: sqlite3.Connection, patient_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM records WHERE patient_key = ? ORDER BY taken_date, id",
        (patient_key,),
    ).fetchall()


# ---------------------------------------------------------------------------
# processed_batches
# ---------------------------------------------------------------------------


def mark_batch(conn: sqlite3.Connection, batch_key: str, state: str) -> None:
    conn.execute(
        """
        INSERT INTO processed_batches(batch_key, state) VALUES (?, ?)
        ON CONFLICT(batch_key) DO UPDATE SET
            state = excluded.state,
            processed_at = datetime('now','localtime')
        """,
        (batch_key, state),
    )
    conn.commit()


def batch_state(conn: sqlite3.Connection, batch_key: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM processed_batches WHERE batch_key = ?", (batch_key,)
    ).fetchone()
    return row["state"] if row is not None else None


# ---------------------------------------------------------------------------
# queue_items
# ---------------------------------------------------------------------------


def add_queue_item(conn: sqlite3.Connection, kind: str, reason: str, payload) -> int:
    """新增待確認佇列項目。payload 可傳 dict（自動 json.dumps）或已序列化的 JSON 字串。"""
    payload_json = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO queue_items(kind, reason, payload) VALUES (?, ?, ?)",
        (kind, reason, payload_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def open_queue_items(conn: sqlite3.Connection, kind: str | None = None) -> list[sqlite3.Row]:
    if kind is None:
        return conn.execute(
            "SELECT * FROM queue_items WHERE state = 'open' ORDER BY created_at, id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM queue_items WHERE state = 'open' AND kind = ? ORDER BY created_at, id",
        (kind,),
    ).fetchall()


def resolve_queue_item(
    conn: sqlite3.Connection, item_id: int, resolution: str, resolved_by: str
) -> None:
    conn.execute(
        """
        UPDATE queue_items
        SET state = 'resolved', resolution = ?, resolved_by = ?, resolved_at = datetime('now','localtime')
        WHERE id = ?
        """,
        (resolution, resolved_by, item_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def add_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    patient_key: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit(actor, action, patient_key, detail) VALUES (?, ?, ?, ?)",
        (actor, action, patient_key, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# users / sessions
# ---------------------------------------------------------------------------


def create_user(conn: sqlite3.Connection, username: str, pwhash: str, role: str) -> None:
    conn.execute(
        "INSERT INTO users(username, pwhash, role) VALUES (?, ?, ?)",
        (username, pwhash, role),
    )
    conn.commit()


def get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def create_session(conn: sqlite3.Connection, token: str, username: str, hours: float) -> str:
    """建立 session，回傳本次配發的 csrf token。

    expires_at 由 SQLite 端以 now + hours 小時算出（與 get_session 用同一時間源）。
    每個 session 綁一枚 ``secrets.token_urlsafe(16)`` 的 csrf token 持久化於同列，
    供 Fix-B 的 web 表單做 double-submit / 標頭比對防 CSRF；get_session 會一併回傳。
    """
    # 用 :+ 強制帶正負號，避免 hours 為負數時組出 "+-1 hours" 這種
    # SQLite 無法解析的 modifier（會被 datetime() 靜默吃成 NULL）。
    csrf = secrets.token_urlsafe(16)
    conn.execute(
        """
        INSERT INTO sessions(token, username, expires_at, csrf)
        VALUES (?, ?, datetime('now','localtime', ?), ?)
        """,
        (token, username, f"{hours:+} hours", csrf),
    )
    conn.commit()
    return csrf


def get_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """取得 session；已過期則刪除並回傳 None（自動過期刪除）。"""
    row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None

    now = conn.execute("SELECT datetime('now','localtime') AS now").fetchone()["now"]
    if row["expires_at"] <= now:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None
    return row
