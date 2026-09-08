"""T2 測試：config.py、db.py、auth.py。

涵蓋：預設生成、損毀重建、DAO round-trip、session 過期、scrypt verify 正反例、
initial admin 只建一次。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import stat
import subprocess
import sys

import pytest

from clinic_archive import auth, config, db


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


def test_load_config_creates_default_when_missing(tmp_path):
    cfg_path = tmp_path / "config.json"

    cfg = config.load_config(cfg_path)

    assert cfg_path.exists()
    assert cfg.data_root == "./clinic_data"
    assert cfg.db_path == "./clinic_data/clinic.db"
    assert cfg.web_host == "0.0.0.0"
    assert cfg.web_port == 8770
    assert cfg.settle_seconds == 10
    assert cfg.poll_seconds == 3
    assert cfg.ocr_version == "PPOCRV6"
    assert cfg.det_side_len == 960
    assert cfg.session_hours == 12
    assert cfg.allowed_exts == [".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"]

    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["data_root"] == "./clinic_data"
    # 磁碟上保留佔位符原文，展開只發生在 load 回傳的物件上。
    assert on_disk["db_path"] == "{data_root}/clinic.db"


def test_load_config_missing_creates_parent_dirs(tmp_path):
    cfg_path = tmp_path / "nested" / "dir" / "config.json"

    config.load_config(cfg_path)

    assert cfg_path.exists()


def test_load_config_expands_data_root_placeholder_for_custom_root(tmp_path):
    cfg_path = tmp_path / "config.json"
    custom_root = str(tmp_path / "custom_clinic_data")
    cfg_path.write_text(json.dumps({"data_root": custom_root}), encoding="utf-8")

    cfg = config.load_config(cfg_path)

    assert cfg.data_root == custom_root
    assert cfg.db_path == f"{custom_root}/clinic.db"


def test_load_config_reads_existing_valid_file_without_rewriting(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"web_port": 9999}), encoding="utf-8")
    before = cfg_path.read_text(encoding="utf-8")

    cfg = config.load_config(cfg_path)

    assert cfg.web_port == 9999
    # 未知/缺漏欄位沿用預設值。
    assert cfg.settle_seconds == 10
    # 檔案已存在且合法時不應被覆寫。
    assert cfg_path.read_text(encoding="utf-8") == before


def test_load_config_rebuilds_on_corrupt_json(tmp_path):
    cfg_path = tmp_path / "config.json"
    corrupt_text = "{ 這不是合法的 JSON"
    cfg_path.write_text(corrupt_text, encoding="utf-8")

    cfg = config.load_config(cfg_path)  # 不得 crash

    backup_path = tmp_path / "config.json.bak"
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == corrupt_text

    rebuilt = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert rebuilt["web_port"] == 8770

    assert cfg.web_port == 8770
    assert cfg.data_root == "./clinic_data"
    assert cfg.db_path == "./clinic_data/clinic.db"


def test_load_config_rebuilds_when_root_is_not_a_json_object(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("[1, 2, 3]", encoding="utf-8")

    cfg = config.load_config(cfg_path)  # 不得 crash

    assert cfg.web_port == 8770
    assert (tmp_path / "config.json.bak").exists()


def test_ensure_dirs_creates_five_subfolders(tmp_path):
    data_root = tmp_path / "clinic_data"
    cfg = config.AppConfig(data_root=str(data_root))

    config.ensure_dirs(cfg)

    for name in ("staging", "inbox", "archive", "review", "trash"):
        sub = data_root / name
        assert sub.is_dir(), f"missing {sub}"


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "clinic.db"
    connection = db.connect(db_path)
    db.init_db(connection)
    yield connection
    connection.close()


def test_connect_sets_wal_and_foreign_keys_and_row_factory(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"

    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1

    row = conn.execute("SELECT 1 AS one").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["one"] == 1


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "clinic2.db"
    connection = db.connect(db_path)
    try:
        db.init_db(connection)
        db.init_db(connection)  # 第二次呼叫不得出錯
    finally:
        connection.close()


def test_patient_upsert_and_get_round_trip(conn):
    db.upsert_patient(conn, "A123456789", name="王小明", dob="2015-01-01", created_by="system")

    patient = db.get_patient(conn, "A123456789")
    assert patient["patient_key"] == "A123456789"
    assert patient["name"] == "王小明"
    assert patient["dob"] == "2015-01-01"
    assert patient["chart_no"] is None

    # 只帶 patient_key 的 upsert 不可用 NULL 蓋掉既有欄位。
    db.upsert_patient(conn, "A123456789")
    still_there = db.get_patient(conn, "A123456789")
    assert still_there["name"] == "王小明"
    assert still_there["dob"] == "2015-01-01"

    # 帶新欄位的 upsert 應該只更新該欄位。
    db.upsert_patient(conn, "A123456789", chart_no="C-001")
    updated = db.get_patient(conn, "A123456789")
    assert updated["chart_no"] == "C-001"
    assert updated["name"] == "王小明"

    assert db.get_patient(conn, "NOBODY") is None


def test_find_patient_by_chart(conn):
    db.upsert_patient(conn, "P-0000001", name="陳小華", chart_no="C-77")

    found = db.find_patient_by_chart(conn, "C-77")
    assert found["patient_key"] == "P-0000001"

    assert db.find_patient_by_chart(conn, "NO-SUCH-CHART") is None


def test_list_patients_search(conn):
    db.upsert_patient(conn, "A123456789", name="王小明")
    db.upsert_patient(conn, "P-0000001", name="陳小華", chart_no="C-77")

    everyone = db.list_patients(conn)
    assert {p["patient_key"] for p in everyone} == {"A123456789", "P-0000001"}

    by_name = db.list_patients(conn, search="小華")
    assert [p["patient_key"] for p in by_name] == ["P-0000001"]

    by_key = db.list_patients(conn, search="A12345")
    assert [p["patient_key"] for p in by_key] == ["A123456789"]

    by_chart = db.list_patients(conn, search="C-77")
    assert [p["patient_key"] for p in by_chart] == ["P-0000001"]

    assert db.list_patients(conn, search="不存在的字串") == []


def test_insert_record_and_records_for_patient_round_trip(conn):
    db.upsert_patient(conn, "A123456789", name="王小明")

    record_id = db.insert_record(
        conn,
        patient_key="A123456789",
        taken_date="2026-07-18",
        rtype="病灶照",
        subtype=None,
        src="phone",
        path="archive/A123456789/2026-07-18_病灶照_01.jpg",
        sha256="deadbeef",
        batch_key="A123456789|2026-07-18|120000",
    )
    assert isinstance(record_id, int)

    records = db.records_for_patient(conn, "A123456789")
    assert len(records) == 1
    assert records[0]["id"] == record_id
    assert records[0]["sha256"] == "deadbeef"
    assert records[0]["status"] == "auto"

    assert db.records_for_patient(conn, "NOBODY") == []


def test_insert_record_requires_existing_patient_fk_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_record(
            conn,
            patient_key="NOPE",
            taken_date="2026-07-18",
            rtype="病灶照",
            subtype=None,
            src="phone",
            path="x.jpg",
            sha256="abc",
        )


def test_batch_mark_and_state_round_trip(conn):
    batch_key = "A123456789|2026-07-18|120000"
    assert db.batch_state(conn, batch_key) is None

    db.mark_batch(conn, batch_key, "auto_filed")
    assert db.batch_state(conn, batch_key) == "auto_filed"

    # 重複 mark 同一鍵（e.g. 遲到檔改標）應該更新狀態而非報錯。
    db.mark_batch(conn, batch_key, "queued")
    assert db.batch_state(conn, batch_key) == "queued"


def test_queue_item_add_open_resolve_round_trip(conn):
    payload = {"files": ["a.jpg", "b.jpg"], "extracted": {}, "batch_key": "~|2026-07-18|120000"}
    item_id = db.add_queue_item(conn, "photo_batch", "首見證號", payload)
    assert isinstance(item_id, int)

    open_items = db.open_queue_items(conn)
    assert len(open_items) == 1
    assert open_items[0]["id"] == item_id
    assert open_items[0]["state"] == "open"
    assert json.loads(open_items[0]["payload"]) == payload

    # kind 篩選
    assert len(db.open_queue_items(conn, kind="photo_batch")) == 1
    assert db.open_queue_items(conn, kind="report") == []

    db.resolve_queue_item(conn, item_id, "confirmed:A123456789", "admin")

    assert db.open_queue_items(conn) == []
    resolved = conn.execute(
        "SELECT * FROM queue_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert resolved["state"] == "resolved"
    assert resolved["resolution"] == "confirmed:A123456789"
    assert resolved["resolved_by"] == "admin"
    assert resolved["resolved_at"] is not None


def test_add_queue_item_accepts_pre_serialized_json_string(conn):
    item_id = db.add_queue_item(conn, "report", "PDF 需人工", json.dumps({"batch_key": None}))
    row = conn.execute("SELECT payload FROM queue_items WHERE id = ?", (item_id,)).fetchone()
    assert json.loads(row["payload"]) == {"batch_key": None}


def test_add_audit_persists_row(conn):
    db.add_audit(conn, actor="admin", action="login")
    db.add_audit(conn, actor="system", action="auto_file", patient_key="A123456789", detail="測試")

    rows = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["actor"] == "admin"
    assert rows[0]["action"] == "login"
    assert rows[0]["patient_key"] is None
    assert rows[1]["patient_key"] == "A123456789"
    assert rows[1]["detail"] == "測試"


def test_create_user_and_get_user_round_trip(conn):
    assert db.get_user(conn, "alice") is None

    db.create_user(conn, "alice", "scrypt$aa$bb", "viewer")
    user = db.get_user(conn, "alice")
    assert user["username"] == "alice"
    assert user["pwhash"] == "scrypt$aa$bb"
    assert user["role"] == "viewer"


def test_create_user_rejects_invalid_role_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.create_user(conn, "bob", "scrypt$aa$bb", "root")


def test_session_round_trip_positive_case(conn):
    db.create_user(conn, "alice", "scrypt$aa$bb", "viewer")

    db.create_session(conn, "live-token", "alice", hours=1)
    row = db.get_session(conn, "live-token")
    assert row is not None
    assert row["username"] == "alice"


def test_session_auto_expires_and_is_deleted(conn):
    db.create_user(conn, "alice", "scrypt$aa$bb", "viewer")

    db.create_session(conn, "expired-token", "alice", hours=-1)

    assert db.get_session(conn, "expired-token") is None

    # 過期後應該真的從資料表刪除，而不是只在讀取時被過濾掉。
    still_there = conn.execute(
        "SELECT 1 FROM sessions WHERE token = ?", ("expired-token",)
    ).fetchone()
    assert still_there is None


def test_get_session_unknown_token_returns_none(conn):
    assert db.get_session(conn, "no-such-token") is None


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------


def test_hash_pw_format():
    stored = auth.hash_pw("correct horse battery staple")
    assert re.match(r"^scrypt\$[0-9a-f]+\$[0-9a-f]+$", stored)
    # 兩次雜湊同一密碼因為 salt 不同，結果不應相同。
    assert auth.hash_pw("correct horse battery staple") != stored


def test_verify_pw_positive_and_negative_cases():
    stored = auth.hash_pw("correct horse battery staple")

    assert auth.verify_pw("correct horse battery staple", stored) is True
    assert auth.verify_pw("wrong password", stored) is False
    assert auth.verify_pw("", stored) is False


def test_verify_pw_rejects_malformed_stored_value_without_raising():
    assert auth.verify_pw("anything", "not-a-valid-format") is False
    assert auth.verify_pw("anything", "md5$deadbeef") is False
    assert auth.verify_pw("anything", "scrypt$not-hex$also-not-hex") is False
    assert auth.verify_pw("anything", "") is False


def test_new_session_and_check_session_round_trip(conn):
    db.create_user(conn, "alice", auth.hash_pw("pw12345"), "viewer")

    token = auth.new_session(conn, "alice", hours=12)
    assert isinstance(token, str)
    assert len(token) > 20

    assert auth.check_session(conn, token) == "alice"


def test_check_session_returns_none_when_expired(conn):
    db.create_user(conn, "alice", auth.hash_pw("pw12345"), "viewer")

    token = auth.new_session(conn, "alice", hours=-1)
    assert auth.check_session(conn, token) is None


def test_check_session_returns_none_for_unknown_token(conn):
    assert auth.check_session(conn, "totally-made-up-token") is None


def test_ensure_initial_admin_creates_admin_once(tmp_path, conn):
    data_root = tmp_path / "clinic_data"
    data_root.mkdir()

    auth.ensure_initial_admin(conn, data_root)

    admin_file = data_root / "FIRST_RUN_ADMIN.txt"
    assert admin_file.exists()

    content = admin_file.read_text(encoding="utf-8")
    match = re.search(r"密碼[:：]\s*(\S+)", content)
    assert match, content
    password = match.group(1)

    user = db.get_user(conn, "admin")
    assert user is not None
    assert user["role"] == "manager"
    assert auth.verify_pw(password, user["pwhash"]) is True

    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert count == 1

    # 再次呼叫必須是 no-op：不新增使用者、不覆寫密碼檔。
    mtime_before = admin_file.stat().st_mtime
    auth.ensure_initial_admin(conn, data_root)

    count_after = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert count_after == 1
    assert admin_file.stat().st_mtime == mtime_before
    assert admin_file.read_text(encoding="utf-8") == content


def test_first_run_admin_uses_icacls_on_windows(tmp_path, conn, monkeypatch, caplog):
    """Windows 上必須改走 icacls 收緊 ACL，而不是無效的 os.chmod。

    os.chmod 在 Windows 只能切唯讀旗標、對 ACL 完全是 no-op，密碼檔等於裸奔。
    這裡假造 win32 平台驗證分支與參數；**icacls 本身無法在 macOS／Linux 驗證**，
    真實 ACL 效果需在 Windows 上人工確認（見工單回報的已知限制）。
    """
    calls = []
    monkeypatch.setattr(auth.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "clinicadmin")
    monkeypatch.setattr(
        auth.os, "chmod",
        lambda *a, **k: pytest.fail("Windows 上不該呼叫 os.chmod（對 ACL 無效）"),
    )

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Proc()

    monkeypatch.setattr(auth.subprocess, "run", fake_run)

    data_root = tmp_path / "clinic_data"
    data_root.mkdir()
    with caplog.at_level(logging.WARNING, logger="clinic_archive.auth"):
        auth.ensure_initial_admin(conn, data_root)

    assert len(calls) == 1, "應剛好呼叫一次 icacls"
    cmd, kwargs = calls[0]
    admin_file = data_root / "FIRST_RUN_ADMIN.txt"
    assert cmd[0] == "icacls"
    assert cmd[1] == str(admin_file)
    assert "/inheritance:r" in cmd          # 砍掉繼承來的寬鬆 ACE
    assert "/grant:r" in cmd                # 取代而非疊加
    assert "clinicadmin:R" in cmd           # 只留目前使用者唯讀
    assert kwargs.get("timeout") == auth._ICACLS_TIMEOUT
    assert kwargs.get("check") is False     # 失敗只警告，不得拋 CalledProcessError

    # 必須明確警告「Windows 上此檔無完整權限保護，讀完立即刪除」。
    assert any("讀完立即刪除" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "outcome",
    [
        "nonzero",       # icacls 回非零（權限不足／非 NTFS）
        "missing",       # icacls 根本不存在
        "timeout",       # icacls 卡住
    ],
)
def test_first_run_admin_survives_icacls_failure(tmp_path, conn, monkeypatch, outcome):
    """icacls 失敗只記警告，絕不中斷首次啟動——收不緊權限也還是要能開機。"""
    monkeypatch.setattr(auth.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "clinicadmin")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "拒絕存取"

    def fake_run(cmd, **kwargs):
        if outcome == "missing":
            raise FileNotFoundError("icacls not found")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, auth._ICACLS_TIMEOUT)
        return _Proc()

    monkeypatch.setattr(auth.subprocess, "run", fake_run)

    data_root = tmp_path / "clinic_data"
    data_root.mkdir()
    auth.ensure_initial_admin(conn, data_root)   # 不得拋例外

    # 帳號與密碼檔仍然正常產生。
    admin_file = data_root / "FIRST_RUN_ADMIN.txt"
    assert admin_file.exists()
    assert db.get_user(conn, "admin") is not None


def test_first_run_admin_skips_icacls_without_username(tmp_path, conn, monkeypatch):
    """取不到 USERNAME 時不硬湊 icacls 參數（會授權給空字串），只警告後略過。"""
    monkeypatch.setattr(auth.sys, "platform", "win32")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setattr(
        auth.subprocess, "run",
        lambda *a, **k: pytest.fail("沒有 USERNAME 就不該呼叫 icacls"),
    )

    data_root = tmp_path / "clinic_data"
    data_root.mkdir()
    auth.ensure_initial_admin(conn, data_root)   # 不得拋例外

    assert (data_root / "FIRST_RUN_ADMIN.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 無 POSIX 權限位元")
def test_first_run_admin_file_is_owner_only(tmp_path, conn):
    """密碼檔必須是 0o600（僅擁有者可讀寫）。

    這條刻意獨立成一個測試並在 Windows 上 skip：NTFS 沒有 POSIX 權限位元，
    `st_mode` 永遠回 0o666/0o444，斷言恆假。Windows 的權限收緊改由
    `auth._restrict_file_permissions` 走 icacls（best-effort，見該函式 docstring），
    無法用 stat 驗證。
    """
    data_root = tmp_path / "clinic_data"
    data_root.mkdir()

    auth.ensure_initial_admin(conn, data_root)

    admin_file = data_root / "FIRST_RUN_ADMIN.txt"
    mode = stat.S_IMODE(admin_file.stat().st_mode)
    assert mode == 0o600


def test_ensure_initial_admin_skips_when_users_already_exist(tmp_path, conn):
    data_root = tmp_path / "clinic_data"
    data_root.mkdir()

    db.create_user(conn, "existing", "scrypt$aa$bb", "viewer")
    auth.ensure_initial_admin(conn, data_root)

    assert db.get_user(conn, "admin") is None
    assert not (data_root / "FIRST_RUN_ADMIN.txt").exists()

    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert count == 1


def test_init_db_migrates_sessions_csrf_from_round1_schema(tmp_path):
    """round-1 舊庫（sessions 無 csrf 欄）→ init_db 自動 ALTER 補欄（審查 R2 P1）。"""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE sessions(token TEXT PRIMARY KEY, "
        "username TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    raw.commit()
    raw.close()
    conn = db.connect(path)
    db.init_db(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert "csrf" in cols
