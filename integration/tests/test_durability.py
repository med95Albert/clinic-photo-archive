"""Fix-C：archiver / 開機耐久性叢集回歸測試。

涵蓋雙審查確認的三個缺陷修法：
  1. ``file_record`` 併發覆寫＝資料遺失（P1）：Lock＋O_CREAT|O_EXCL 雙保險。
  2. 搬檔後崩潰＝孤兒檔（P1）：``archiver.reconcile`` 開機耐久性巡檢。
  3. ``merge_patient`` 對不存在 to_key 的 FK 炸裂風險：提前 ``ValueError``。
  外加 run.py 啟動器（editable install .pth 未生效問題）與 main.bootstrap 的
  reconcile 接線各一個輕量驗證。

本檔刻意不 import ``clinic_archive.db``：db.py 正由並行工單修改中，本檔自帶最小
DDL（與 ``tests/test_archiver.py`` 相同慣例），不依賴它本輪的新介面。也完全不碰
``predicate.py``／``reports.py``。``tests/test_archiver.py`` 本身不動，新測試都在
這裡。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from clinic_archive import archiver
from clinic_archive import main as main_mod

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
CREATE TABLE IF NOT EXISTS queue_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  resolution TEXT, resolved_by TEXT, resolved_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  actor TEXT NOT NULL, action TEXT NOT NULL,
  patient_key TEXT, detail TEXT);
"""

PID = "A123456789"
TO_KEY = "B223456782"


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


def mksrc(staging_dir, name, data):
    staging_dir.mkdir(parents=True, exist_ok=True)
    p = staging_dir / name
    p.write_bytes(data)
    return p


def archive_dir(cfg, key):
    return Path(cfg.data_root) / "archive" / key


def review_dir(cfg):
    return Path(cfg.data_root) / "review"


def open_orphan_reasons(conn) -> list[str]:
    rows = conn.execute(
        "SELECT reason FROM queue_items WHERE kind='orphan' AND state='open' ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


# ===========================================================================
# 1) file_record 併發覆寫＝資料遺失（P1）
# ===========================================================================
def _open_disk_db(tmp_path) -> Path:
    """建一個實體檔 sqlite db（併發測試每條執行緒各自開連線，不可共用單一連線）。"""
    db_path = tmp_path / "clinic.db"
    setup = sqlite3.connect(str(db_path))
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("PRAGMA foreign_keys=ON")
    setup.executescript(DDL)
    setup.execute(
        "INSERT INTO patients(patient_key, name, dob) VALUES(?,?,?)",
        (PID, "王小明", "2000-01-01"),
    )
    setup.commit()
    setup.close()
    return db_path


def test_concurrent_file_record_no_loss_no_overwrite(tmp_path):
    """thread pool 併發寫同一 patient/date/type：檔案數==紀錄數、sha 相符、無覆寫。

    對應已重現的 P1 缺陷：12 併發寫入只剩 2-4 檔（序號計算＋目的檔宣告非原子，
    後寫入者的 os.replace 悄悄蓋掉先寫入者剛搬進去的檔）。本測試用 16 條執行緒
    （> 缺陷單張規定的 >= 8）驗證修法後不再發生。
    """
    root = tmp_path / "clinic_data"
    (root / "archive").mkdir(parents=True)
    (root / "review").mkdir(parents=True)
    cfg = types.SimpleNamespace(data_root=str(root))
    db_path = _open_disk_db(tmp_path)
    staging = tmp_path / "staging"

    N = 16

    def worker(i: int):
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            data = f"payload-{i}-".encode() * 50  # 每條執行緒內容互異，可用 sha 分辨
            src = mksrc(staging, f"shot_{i}.jpg", data)
            dest = archiver.file_record(
                conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", None,
            )
            return str(dest), hashlib.sha256(data).hexdigest()
        finally:
            conn.close()

    results = []
    with ThreadPoolExecutor(max_workers=N) as ex:
        futs = [ex.submit(worker, i) for i in range(N)]
        for f in as_completed(futs):
            results.append(f.result())

    # 磁碟上：檔案數量吻合、路徑（檔名）各自唯一 → 無覆寫、無遺失。
    patient_dir = archive_dir(cfg, PID)
    files_on_disk = [p for p in patient_dir.iterdir() if p.is_file()]
    assert len(files_on_disk) == N
    assert len({p.name for p in files_on_disk}) == N

    # DB 上：紀錄數量吻合、path 各自唯一。
    check_conn = sqlite3.connect(str(db_path))
    rows = check_conn.execute(
        "SELECT path, sha256 FROM records WHERE patient_key=?", (PID,)
    ).fetchall()
    check_conn.close()
    assert len(rows) == N
    db_paths = [r[0] for r in rows]
    assert len(set(db_paths)) == N

    # 每筆 DB 記錄的 sha256 與磁碟實際內容相符（無交叉污染）。
    for path, sha in rows:
        p = Path(path)
        assert p.exists(), f"records.path 指向的檔案不存在：{path}"
        assert hashlib.sha256(p.read_bytes()).hexdigest() == sha

    # worker 回傳的 (path, sha) 與 DB 一致：確認每條執行緒都拿到自己專屬的目的路徑。
    result_by_path = dict(results)
    assert len(result_by_path) == N  # worker 回傳的 dest 路徑本身也應該互不相同
    for path, sha in rows:
        assert result_by_path[path] == sha


def test_file_record_bumps_suffix_when_seq_slot_occupied(conn, cfg, tmp_path):
    """existing 檔名序號不連續、算出的 seq 撞到既有檔 → O_CREAT|O_EXCL 觸發 -2 後綴。

    直接練到 _claim_destination 的 FileExistsError bump 分支，而非只靠併發計時
    間接命中：手動製造「01 與 03 存在、02 缺號」的狀態，existing 計數為 2 使得
    算出的 seq=3，恰好撞上既有的 03 檔，逼出後綴 bump，且確認撞到的既有檔內容
    毫髮無傷（絕不覆蓋）。
    """
    add_patient(conn, PID, dob="2000-01-01")
    pdir = archive_dir(cfg, PID)
    pdir.mkdir(parents=True)
    (pdir / "2026-07-18_病灶照_01.jpg").write_bytes(b"one")
    (pdir / "2026-07-18_病灶照_03.jpg").write_bytes(b"three")

    src = mksrc(tmp_path / "staging", "new.jpg", b"new-content")
    dest = archiver.file_record(
        conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", None,
    )

    assert dest.name == "2026-07-18_病灶照_03-2.jpg"
    assert (pdir / "2026-07-18_病灶照_03.jpg").read_bytes() == b"three"
    assert dest.read_bytes() == b"new-content"


# ===========================================================================
# 2) reconcile()：開機耐久性巡檢，三種孤兒情境
# ===========================================================================
def test_reconcile_flags_archive_file_without_record(conn, cfg):
    """archive/ 有實體檔但 records 表無對應列 → orphan('歸檔區發現無索引檔案')。"""
    pdir = archive_dir(cfg, PID)
    pdir.mkdir(parents=True)
    orphan = pdir / "2026-07-18_病灶照_01.jpg"
    orphan.write_bytes(b"no-record-for-me")

    counts = archiver.reconcile(conn, cfg)

    assert counts["archive_orphan_files"] == 1
    assert counts["archive_missing_records"] == 0
    assert counts["review_orphan_files"] == 0
    assert open_orphan_reasons(conn) == ["歸檔區發現無索引檔案"]

    row = conn.execute(
        "SELECT payload FROM queue_items WHERE kind='orphan'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["files"] == [str(orphan)]

    # 冪等：同一路徑再跑一次不重複建立。
    counts2 = archiver.reconcile(conn, cfg)
    assert counts2 == {
        "archive_orphan_files": 0,
        "archive_missing_records": 0,
        "review_orphan_files": 0,
        "stale_processing_reverted": 0,
    }
    assert len(open_orphan_reasons(conn)) == 1


def test_reconcile_flags_record_with_missing_file(conn, cfg):
    """records.path 指向的檔案在磁碟上不存在 → orphan('索引指向的檔案遺失')。"""
    add_patient(conn, PID, dob="2000-01-01")
    missing_path = str(archive_dir(cfg, PID) / "2026-07-18_檢驗-CBC_01.jpg")
    conn.execute(
        "INSERT INTO records(patient_key, taken_date, rtype, subtype, src, path, sha256) "
        "VALUES(?,?,?,?,?,?,?)",
        (PID, "2026-07-18", "檢驗", "CBC", "inbox", missing_path, "deadbeef" * 8),
    )
    conn.commit()

    counts = archiver.reconcile(conn, cfg)

    assert counts["archive_missing_records"] == 1
    assert counts["archive_orphan_files"] == 0
    assert open_orphan_reasons(conn) == ["索引指向的檔案遺失"]

    row = conn.execute(
        "SELECT payload FROM queue_items WHERE kind='orphan'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["files"] == [missing_path]
    assert payload["patient_key"] == PID

    # 冪等。
    counts2 = archiver.reconcile(conn, cfg)
    assert counts2["archive_missing_records"] == 0
    assert len(open_orphan_reasons(conn)) == 1


def test_reconcile_flags_unreferenced_review_file_only(conn, cfg):
    """review/ 孤兒檔判準：只有「不被任何 open 佇列項引用」的才算孤兒。

    同時放一個有主（被某個 open photo_batch 佇列項的 payload.files 引用）的檔案，
    確認它不會被誤判為孤兒——避免 reconcile 對整個 review/ 見檔就抓。
    """
    rdir = review_dir(cfg)
    rdir.mkdir(parents=True, exist_ok=True)
    claimed = rdir / "claimed.jpg"
    claimed.write_bytes(b"claimed")
    orphan = rdir / "orphan.jpg"
    orphan.write_bytes(b"orphan")

    conn.execute(
        "INSERT INTO queue_items(kind, reason, payload, state) VALUES(?,?,?,?)",
        ("photo_batch", "首見證號", json.dumps({"files": [str(claimed)]}), "open"),
    )
    conn.commit()

    counts = archiver.reconcile(conn, cfg)

    assert counts["review_orphan_files"] == 1
    reasons = conn.execute(
        "SELECT reason, payload FROM queue_items WHERE kind='orphan'"
    ).fetchall()
    assert len(reasons) == 1
    assert reasons[0][0] == "待確認區發現無主檔案"
    assert json.loads(reasons[0][1])["files"] == [str(orphan)]

    # 冪等。
    counts2 = archiver.reconcile(conn, cfg)
    assert counts2["review_orphan_files"] == 0
    assert len(open_orphan_reasons(conn)) == 1


def test_reconcile_all_three_scenarios_together_idempotent(conn, cfg):
    """三種孤兒情境同時存在時，各自正確計數，且整體對第二次呼叫冪等。"""
    add_patient(conn, PID, dob="2000-01-01")

    pdir = archive_dir(cfg, PID)
    pdir.mkdir(parents=True)
    (pdir / "2026-07-18_病灶照_01.jpg").write_bytes(b"orphan-on-disk")

    missing_path = str(pdir / "2026-07-19_檢驗-CBC_01.jpg")
    conn.execute(
        "INSERT INTO records(patient_key, taken_date, rtype, subtype, src, path, sha256) "
        "VALUES(?,?,?,?,?,?,?)",
        (PID, "2026-07-19", "檢驗", "CBC", "inbox", missing_path, "cafebabe" * 8),
    )
    conn.commit()

    rdir = review_dir(cfg)
    (rdir / "lost.jpg").write_bytes(b"lost-in-review")

    counts = archiver.reconcile(conn, cfg)
    assert counts == {
        "archive_orphan_files": 1,
        "archive_missing_records": 1,
        "review_orphan_files": 1,
        "stale_processing_reverted": 0,
    }
    assert len(open_orphan_reasons(conn)) == 3

    counts2 = archiver.reconcile(conn, cfg)
    assert counts2 == {
        "archive_orphan_files": 0,
        "archive_missing_records": 0,
        "review_orphan_files": 0,
        "stale_processing_reverted": 0,
    }
    assert len(open_orphan_reasons(conn)) == 3  # 沒有重複灌注


# ===========================================================================
# 3) merge_patient：to_key 不存在 → ValueError（防 FK 炸裂）
# ===========================================================================
def test_merge_patient_raises_value_error_for_missing_to_key(conn, cfg, tmp_path):
    add_patient(conn, PID, dob="2000-01-01")
    missing_to_key = "Z999999999"
    assert conn.execute(
        "SELECT 1 FROM patients WHERE patient_key=?", (missing_to_key,)
    ).fetchone() is None

    with pytest.raises(ValueError, match=missing_to_key):
        archiver.merge_patient(conn, cfg, PID, missing_to_key, actor="mgr")


def test_merge_patient_missing_to_key_has_no_side_effects(conn, cfg, tmp_path):
    """驗證檢查發生在任何檔案搬移之前：ValueError 後 from_key 的檔案與 records 原封不動。"""
    add_patient(conn, PID, dob="2000-01-01")
    src = mksrc(tmp_path / "staging", "a.jpg", b"data")
    archiver.file_record(conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", None)

    from_dir = archive_dir(cfg, PID)
    files_before = sorted(p.name for p in from_dir.iterdir())
    records_before = conn.execute(
        "SELECT patient_key, path FROM records ORDER BY id"
    ).fetchall()

    with pytest.raises(ValueError):
        archiver.merge_patient(conn, cfg, PID, "NOTEXIST999", actor="mgr")

    assert from_dir.exists()
    assert sorted(p.name for p in from_dir.iterdir()) == files_before
    records_after = conn.execute(
        "SELECT patient_key, path FROM records ORDER BY id"
    ).fetchall()
    assert records_after == records_before


def test_merge_patient_succeeds_when_to_key_exists(conn, cfg, tmp_path):
    """正面案例：to_key 存在時完全不受此修法影響，行為與既有 test_archiver.py 一致。"""
    add_patient(conn, PID, dob="2000-01-01")
    add_patient(conn, TO_KEY, dob="2000-01-01")
    src = mksrc(tmp_path / "staging", "a.jpg", b"data")
    archiver.file_record(conn, cfg, src, PID, "2026-07-18", "病灶照", None, "phone", None)

    archiver.merge_patient(conn, cfg, PID, TO_KEY, actor="mgr")

    assert not archive_dir(cfg, PID).exists()
    rows = conn.execute("SELECT patient_key FROM records").fetchall()
    assert all(r[0] == TO_KEY for r in rows)


# ===========================================================================
# 4) run.py 啟動器：任意工作目錄都能正確 import 套件
# ===========================================================================
def test_run_py_works_from_unrelated_cwd(tmp_path):
    """python run.py --help 從跟 integration/ 無關的工作目錄呼叫仍要能正確載入套件。

    用 --help 讓 argparse 印完用法就 SystemExit(0)，不會真的起 watcher／web server，
    測試才能又快又不需要網路埠。
    """
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    assert run_py.is_file(), "run.py 應該在 integration/ 根層"

    result = subprocess.run(
        [sys.executable, str(run_py), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "--config" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


# ===========================================================================
# 5) main.bootstrap()：init 完成後接線呼叫 reconcile 並 log 統計
# ===========================================================================
@pytest.fixture
def clean_root_logging():
    """main.bootstrap() 會呼叫 _setup_logging() 對全域 root logger 掛 handler。

    測試後還原 handler 清單與 level，避免汙染同一個 pytest 行程裡其他測試的
    log 輸出（例如遺留指向已刪除 tmp_path 的 RotatingFileHandler）。
    """
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    yield
    for h in list(root.handlers):
        if h not in handlers_before:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    root.setLevel(level_before)


def test_bootstrap_runs_reconcile_and_logs_stats(tmp_path, clean_root_logging, caplog):
    """bootstrap() 在 init_db／ensure_initial_admin 之後接著跑 reconcile 並 log 統計。

    預先在 data_root/archive 底下塞一個孤兒檔，bootstrap 完成後應該能在 DB 裡
    找到對應的 orphan queue_item，且 INFO log 有印出統計（驗證 _setup_logging
    確實在 reconcile 呼叫之前就位，log 不會被靜默吞掉——見 main.py bootstrap()
    docstring 的說明）。
    """
    data_root = tmp_path / "clinic_data"
    archive_pdir = data_root / "archive" / PID
    archive_pdir.mkdir(parents=True)
    orphan_file = archive_pdir / "2026-07-18_病灶照_01.jpg"
    orphan_file.write_bytes(b"orphan-before-boot")

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"data_root": str(data_root)}, ensure_ascii=False), encoding="utf-8"
    )

    caplog.set_level(logging.INFO, logger="clinic_archive.main")

    cfg = main_mod.bootstrap(str(config_path))

    assert Path(cfg.data_root) == data_root

    check_conn = sqlite3.connect(str(Path(cfg.db_path)))
    rows = check_conn.execute(
        "SELECT reason FROM queue_items WHERE kind='orphan' AND state='open'"
    ).fetchall()
    check_conn.close()
    assert ("歸檔區發現無索引檔案",) in rows

    assert "開機耐久性巡檢完成" in caplog.text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_claimed_move_never_overwrites_same_basename(tmp_path):
    """兩個同名來源檔搬進同一目的資料夾 → 一個原名一個 -2 後綴，內容都在（R2 P1）。"""
    d1 = tmp_path / "a"; d2 = tmp_path / "b"; dest = tmp_path / "trash"
    d1.mkdir(); d2.mkdir()
    f1 = d1 / "card.jpg"; f1.write_bytes(b"AAA")
    f2 = d2 / "card.jpg"; f2.write_bytes(b"BBB")
    p1 = archiver.claimed_move(f1, dest)
    p2 = archiver.claimed_move(f2, dest)
    assert p1 != p2
    assert {p.read_bytes() for p in (p1, p2)} == {b"AAA", b"BBB"}
    assert not f1.exists() and not f2.exists()


def test_reconcile_reverts_stale_processing_claims(conn, cfg):
    """卡在 resolved/'processing' 的殭屍認領 → reconcile 退回 open（R2 P1）。"""
    cur = conn.execute(
        "INSERT INTO queue_items(kind, reason, payload) VALUES ('photo_batch','測試','{}')"
    )
    item_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE queue_items SET state='resolved', resolved_by='boss', "
        "resolved_at=datetime('now','localtime'), resolution='processing' WHERE id=?",
        (item_id,),
    )
    conn.commit()
    counts = archiver.reconcile(conn, cfg)
    assert counts["stale_processing_reverted"] == 1
    state, resolution = conn.execute(
        "SELECT state, resolution FROM queue_items WHERE id=?", (item_id,)
    ).fetchone()
    assert state == "open" and resolution is None


def test_reconcile_reverts_zombie_before_review_scan(conn, cfg, tmp_path):
    """殭屍認領引用的 review 檔不得被誤掛 orphan——退回必須先於掃描（R3 P0）。"""
    rdir = Path(cfg.data_root) / "review"
    rdir.mkdir(parents=True, exist_ok=True)
    f = rdir / "claimed.jpg"
    f.write_bytes(b"X")
    cur = conn.execute(
        "INSERT INTO queue_items(kind, reason, payload, state, resolution) "
        "VALUES ('photo_batch','測試', ?, 'resolved', 'processing')",
        (f'{{"files": ["{f}"]}}',),
    )
    item_id = int(cur.lastrowid)
    conn.commit()
    counts = archiver.reconcile(conn, cfg)
    assert counts["stale_processing_reverted"] == 1
    assert counts["review_orphan_files"] == 0
    state = conn.execute(
        "SELECT state FROM queue_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    assert state == "open"
    orphans = conn.execute(
        "SELECT COUNT(*) FROM queue_items WHERE kind='orphan'"
    ).fetchone()[0]
    assert orphans == 0
