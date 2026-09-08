"""archiver 實體搬移 + DB 一致性測試（SPEC §9）。

以 tmp_path 建真實檔案、以 sqlite（foreign_keys ON）驗 records/audit/patients，
涵蓋：原子搬移（來源消失、目的存在）、重名後綴、seq 遞增、跨磁碟 fallback、
merge/rename 後 DB 一致。
"""

import errno
import hashlib
import os
import sqlite3
import types
from pathlib import Path

import pytest

from clinic_archive import archiver

DDL = """
CREATE TABLE IF NOT EXISTS patients(
  patient_key TEXT PRIMARY KEY,
  name TEXT, dob TEXT, chart_no TEXT,
  created_by TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_key TEXT NOT NULL REFERENCES patients(patient_key),
  taken_date TEXT NOT NULL, rtype TEXT NOT NULL, subtype TEXT,
  src TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'auto', batch_key TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  actor TEXT NOT NULL, action TEXT NOT NULL,
  patient_key TEXT, detail TEXT);
"""

PID = "A123456789"


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "clinic_data"
    (root / "archive").mkdir(parents=True)
    (root / "review").mkdir(parents=True)
    return types.SimpleNamespace(data_root=str(root))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(DDL)
    return c


def add_patient(conn, key, dob=None, chart_no=None, name="王小明"):
    conn.execute(
        "INSERT INTO patients(patient_key, name, dob, chart_no) VALUES(?,?,?,?)",
        (key, name, dob, chart_no),
    )
    conn.commit()


def mksrc(tmp_path, name="shot.jpg", data=b"IMG-DATA"):
    src_dir = tmp_path / "staging"
    src_dir.mkdir(parents=True, exist_ok=True)
    p = src_dir / name
    p.write_bytes(data)
    return p


def archive_dir(cfg, key):
    return Path(cfg.data_root) / "archive" / key


# ---- file_record：原子搬移 + naming + DB ---------------------------------
def test_file_record_moves_and_records(conn, cfg, tmp_path):
    add_patient(conn, PID, dob="2000-01-01")
    data = b"lesion-photo-bytes"
    src = mksrc(tmp_path, "IMG_0001.JPG", data)

    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone",
        "A123456789|2026-07-18|101500",
    )

    # 原子性：來源消失、目的存在
    assert not src.exists()
    assert dest.exists()
    assert dest.read_bytes() == data
    # naming：archive/{key}/{date}_{rtype}_{seq:02d}{ext}，副檔名小寫
    assert dest.parent == archive_dir(cfg, PID)
    assert dest.name == "2026-07-18_病灶照_01.jpg"

    row = conn.execute(
        "SELECT patient_key, taken_date, rtype, subtype, src, path, sha256, status, batch_key "
        "FROM records"
    ).fetchone()
    assert row[0] == PID
    assert row[1] == "2026-07-18"
    assert row[2] == "病灶照"
    assert row[3] is None
    assert row[4] == "phone"
    assert row[5] == str(dest)
    assert row[6] == hashlib.sha256(data).hexdigest()
    assert row[7] == "auto"
    assert row[8] == "A123456789|2026-07-18|101500"

    audit = conn.execute(
        "SELECT actor, action, patient_key, detail FROM audit"
    ).fetchone()
    assert audit == ("system", "auto_file", PID, "2026-07-18_病灶照_01.jpg")


def test_file_record_subtype_in_name(conn, cfg, tmp_path):
    add_patient(conn, PID, dob="2000-01-01")
    src = mksrc(tmp_path, "rep.png", b"cbc")
    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "檢驗", "CBC", "inbox", None,
    )
    assert dest.name == "2026-07-18_檢驗-CBC_01.png"


def test_file_record_seq_increments(conn, cfg, tmp_path):
    add_patient(conn, PID, dob="2000-01-01")
    names = []
    for i in range(3):
        src = mksrc(tmp_path, f"s{i}.jpg", f"d{i}".encode())
        dest = archiver.file_record(
            conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", "bk",
        )
        names.append(dest.name)
    assert names == [
        "2026-07-18_病灶照_01.jpg",
        "2026-07-18_病灶照_02.jpg",
        "2026-07-18_病灶照_03.jpg",
    ]
    # 同型不同日 → seq 重新起算
    src = mksrc(tmp_path, "other.jpg", b"x")
    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-19", "病灶照", None, "phone", "bk",
    )
    assert dest.name == "2026-07-19_病灶照_01.jpg"


def test_file_record_actor_custom(conn, cfg, tmp_path):
    add_patient(conn, PID, dob="2000-01-01")
    src = mksrc(tmp_path, "a.jpg", b"z")
    archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", "bk",
        actor="alice",
    )
    assert conn.execute("SELECT actor FROM audit").fetchone()[0] == "alice"


def test_file_record_dedup_on_preexisting(conn, cfg, tmp_path):
    # 手動在患者夾放一個佔用 _01 slot 的非 record 檔，逼出 -2/seq 邏輯
    add_patient(conn, PID, dob="2000-01-01")
    pdir = archive_dir(cfg, PID)
    pdir.mkdir(parents=True)
    (pdir / "2026-07-18_病灶照_01.jpg").write_bytes(b"pre")
    src = mksrc(tmp_path, "new.jpg", b"new")
    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", "bk",
    )
    # 既有 1 檔 → seq=2，且不覆蓋既有檔
    assert dest.name == "2026-07-18_病灶照_02.jpg"
    assert (pdir / "2026-07-18_病灶照_01.jpg").read_bytes() == b"pre"


# ---- move_to_review ------------------------------------------------------
def test_move_to_review_preserves_name(cfg, tmp_path):
    src = mksrc(tmp_path, "weird thing.jpg", b"q")
    dest = archiver.move_to_review(cfg, src)
    assert not src.exists()
    assert dest.parent == Path(cfg.data_root) / "review"
    assert dest.name == "weird thing.jpg"


def test_move_to_review_collision_suffix(cfg, tmp_path):
    d1 = archiver.move_to_review(cfg, mksrc(tmp_path, "dup.jpg", b"a"))
    d2 = archiver.move_to_review(cfg, mksrc(tmp_path, "dup.jpg", b"b"))
    d3 = archiver.move_to_review(cfg, mksrc(tmp_path, "dup.jpg", b"c"))
    assert d1.name == "dup.jpg"
    assert d2.name == "dup-2.jpg"
    assert d3.name == "dup-3.jpg"
    assert d2.read_bytes() == b"b"


# ---- 跨磁碟 fallback（copy+fsync+unlink）--------------------------------
def test_move_cross_device_fallback(conn, cfg, tmp_path, monkeypatch):
    add_patient(conn, PID, dob="2000-01-01")
    data = b"cross-device-bytes"
    src = mksrc(tmp_path, "x.jpg", data)

    real_replace = os.replace
    state = {"n": 0}

    def fake_replace(a, b):
        state["n"] += 1
        if state["n"] == 1:                      # 首次 src->dest 佯裝跨磁碟
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)                # tmp->dest 用真的

    monkeypatch.setattr(archiver.os, "replace", fake_replace)
    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", "bk",
    )
    assert not src.exists()
    assert dest.exists()
    assert dest.read_bytes() == data
    # 不留 .part 殘檔
    assert not (dest.parent / (dest.name + ".part")).exists()


# ---- merge_patient -------------------------------------------------------
def test_merge_patient_moves_files_and_repoints(conn, cfg, tmp_path):
    to_key = "B223456782"
    add_patient(conn, PID, dob="2000-01-01")
    add_patient(conn, to_key, dob="2000-01-01")

    # from 病人有兩筆歸檔
    for i in range(2):
        src = mksrc(tmp_path, f"f{i}.jpg", f"from{i}".encode())
        archiver.file_record(conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", "bk")
    # to 病人先有一筆（製造 merge 後同名碰撞可能）
    src = mksrc(tmp_path, "t.jpg", b"to0")
    archiver.file_record(conn, cfg, src, to_key, "2026-07-18", "病灶照", None, "phone", "bk")

    from_dir = archive_dir(cfg, PID)
    archiver.merge_patient(conn, cfg, PID, to_key, actor="mgr")

    # from 資料夾清空移除、檔案都在 to 夾
    assert not from_dir.exists()
    to_dir = archive_dir(cfg, to_key)
    assert len(list(to_dir.iterdir())) == 3

    # records 全數改到 to_key，且路徑指向存在的檔
    rows = conn.execute("SELECT patient_key, path FROM records").fetchall()
    assert all(r[0] == to_key for r in rows)
    assert all(Path(r[1]).exists() for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) FROM records WHERE patient_key=?", (PID,)
    ).fetchone()[0] == 0

    # audit('merge')
    assert conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='merge' AND patient_key=?", (to_key,)
    ).fetchone()[0] == 1


# ---- rename_patient_key（P-碼補證號）------------------------------------
def test_rename_patient_key_whole_dir(conn, cfg, tmp_path):
    old = "P-0000001"
    new = PID
    add_patient(conn, old, dob="2000-01-01", chart_no="CH9")
    for i in range(2):
        src = mksrc(tmp_path, f"p{i}.jpg", f"pp{i}".encode())
        archiver.file_record(conn, cfg, src, old, "2026-07-18", "病灶照", None, "phone", "bk")

    archiver.rename_patient_key(conn, cfg, old, new, actor="mgr")

    # 舊夾不存在、新夾有兩檔
    assert not archive_dir(cfg, old).exists()
    new_dir = archive_dir(cfg, new)
    assert len(list(new_dir.iterdir())) == 2

    # patients：舊 key 消失、新 key 承接欄位（chart_no 沿用）
    assert conn.execute(
        "SELECT COUNT(*) FROM patients WHERE patient_key=?", (old,)
    ).fetchone()[0] == 0
    prow = conn.execute(
        "SELECT dob, chart_no FROM patients WHERE patient_key=?", (new,)
    ).fetchone()
    assert prow == ("2000-01-01", "CH9")

    # records：全改新 key，路徑指向新夾且存在
    rows = conn.execute("SELECT patient_key, path FROM records").fetchall()
    assert all(r[0] == new for r in rows)
    assert all(Path(r[1]).exists() for r in rows)
    assert all(str(new_dir) in r[1] for r in rows)

    assert conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='reassign' AND patient_key=?", (new,)
    ).fetchone()[0] == 1


def test_rename_patient_key_merge_into_existing(conn, cfg, tmp_path):
    # 目的 key 已存在資料夾 → 逐檔併入（走 _relocate_dir 分支）
    old = "P-0000002"
    new = PID
    add_patient(conn, old, dob="2000-01-01")
    add_patient(conn, new, dob="2000-01-01")
    src = mksrc(tmp_path, "old.jpg", b"old")
    archiver.file_record(conn, cfg, src, old, "2026-07-18", "病灶照", None, "phone", "bk")
    src = mksrc(tmp_path, "exist.jpg", b"exist")
    archiver.file_record(conn, cfg, src, new, "2026-07-18", "病灶照", None, "phone", "bk")

    archiver.rename_patient_key(conn, cfg, old, new, actor="mgr")

    new_dir = archive_dir(cfg, new)
    assert not archive_dir(cfg, old).exists()
    assert len(list(new_dir.iterdir())) == 2  # 既有 + 併入
    rows = conn.execute("SELECT patient_key, path FROM records").fetchall()
    assert all(r[0] == new for r in rows)
    assert all(Path(r[1]).exists() for r in rows)


# ---- _move：跨磁碟目錄搬移 -------------------------------------------------
def test_move_dir_cross_device_uses_shutil_not_open(tmp_path, monkeypatch):
    """整夾更名遇到跨磁碟時必須走 shutil.move。

    舊 fallback 是 copy+fsync+unlink，會對目錄 ``open(src, "rb")``——Linux 拋
    IsADirectoryError、Windows 拋 PermissionError。rename_patient_key 的整夾更名
    傳的正是目錄，所以 archive/ 一旦跨磁碟，P-碼補證號就整個爆掉。
    """
    src_dir = tmp_path / "P-0000001"
    src_dir.mkdir()
    (src_dir / "a.jpg").write_bytes(b"aa")
    (src_dir / "b.jpg").write_bytes(b"bb")
    dest_dir = tmp_path / "A123456789"

    def fake_replace(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(archiver.os, "replace", fake_replace)
    archiver._move(src_dir, dest_dir)

    assert not src_dir.exists()
    assert (dest_dir / "a.jpg").read_bytes() == b"aa"
    assert (dest_dir / "b.jpg").read_bytes() == b"bb"


def test_move_dir_cross_device_refuses_existing_dest(tmp_path, monkeypatch):
    """跨磁碟整夾搬移不得把 src 塞進既有的 dest（會產生巢狀 archive/新/舊/）。"""
    src_dir = tmp_path / "P-0000001"
    src_dir.mkdir()
    (src_dir / "a.jpg").write_bytes(b"aa")
    dest_dir = tmp_path / "A123456789"
    dest_dir.mkdir()

    monkeypatch.setattr(
        archiver.os, "replace",
        lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )
    with pytest.raises(FileExistsError):
        archiver._move(src_dir, dest_dir)

    assert (src_dir / "a.jpg").exists()          # 來源原封不動
    assert not (dest_dir / "P-0000001").exists()  # 沒有搬出巢狀結構


# ---- _move：Windows 鎖檔的有界重試 ----------------------------------------
def test_move_retries_transient_permission_error(tmp_path, monkeypatch):
    """Defender／索引器短暫鎖檔（winerror 32）→ 重試後成功，不該直接拋。"""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest.jpg"

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] <= 3:                       # 前 3 次佯裝被鎖住
            err = PermissionError(errno.EACCES, "被 Defender 鎖住")
            err.winerror = 32                     # ERROR_SHARING_VIOLATION
            raise err
        return real_replace(a, b)

    sleeps: list[float] = []
    monkeypatch.setattr(archiver.os, "replace", flaky_replace)
    monkeypatch.setattr(archiver.time, "sleep", sleeps.append)

    archiver._move(src, dest)

    assert calls["n"] == 4
    assert dest.read_bytes() == b"payload"
    assert not src.exists()
    assert sleeps == pytest.approx([0.3, 0.6, 0.9])   # 漸增，且有界


def test_move_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    """一直鎖著就要如實拋出——有界重試，絕不無限重試也絕不無聲吞掉。"""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest.jpg"

    calls = {"n": 0}

    def always_locked(a, b):
        calls["n"] += 1
        err = PermissionError(errno.EACCES, "永遠被鎖住")
        err.winerror = 32
        raise err

    sleeps: list[float] = []
    monkeypatch.setattr(archiver.os, "replace", always_locked)
    monkeypatch.setattr(archiver.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        archiver._move(src, dest)

    assert calls["n"] == archiver._MOVE_RETRY_ATTEMPTS   # 有界
    assert len(sleeps) == archiver._MOVE_RETRY_ATTEMPTS - 1
    assert src.exists()                                   # 失敗時來源保留


def test_move_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    """非鎖檔的 OSError（如 ENOENT）要立刻拋，不能浪費 5 輪重試。"""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"payload")
    dest = tmp_path / "dest.jpg"

    calls = {"n": 0}

    def enoent(a, b):
        calls["n"] += 1
        raise OSError(errno.ENOENT, "no such file")

    monkeypatch.setattr(archiver.os, "replace", enoent)
    monkeypatch.setattr(archiver.time, "sleep", lambda s: pytest.fail("不該睡"))

    with pytest.raises(OSError):
        archiver._move(src, dest)

    assert calls["n"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
