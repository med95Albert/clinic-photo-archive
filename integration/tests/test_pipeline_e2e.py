"""管線端對端測試（SPEC §10 的 test_pipeline_e2e.py）。

模擬 ClinicSnap 手機端逐檔寫入 ``staging/`` 與桌面掃描落 ``inbox/`` 的行為，跑一輪
``process_staging`` / ``process_inbox`` / ``watcher.run_once``，斷言 archive／review
的**實體檔案位置**與 DB 的 records／queue_items／processed_batches／audit 狀態。

**不需真 OCR 引擎**：以 monkeypatch 換掉 ``clinic_archive.ocr.ocr_image_text``，讓「影像檔」
的檔案內容直接當成 OCR 逐行文字回傳（fake_ocr 讀檔文字）。**不啟 web**：只驗管線與巡檢一輪。

靜置窗以 ``cfg.settle_seconds = 0`` 控制（不 sleep）；watcher 執行緒測試另以自身連線
驗證跨執行緒可見性（整合注意 1：連線不跨執行緒共用）。

涵蓋情境（整合注意 6）：正常有卡批自動歸檔、碰撞後綴混批佇列、無卡手動 pid 批佇列、
首見證號佇列、inbox 報告 auto 與 queue 各一、PDF 進佇列、非白名單副檔名佇列、遲到檔。
"""

import threading
import time
from pathlib import Path

import pytest

from clinic_archive import config, db, ocr, reports, watcher
from clinic_archive.batching import REASON_MIXED, REASON_STRAGGLER

# --- 已建檔病人（真內政部 checksum 有效號）--------------------------------
PID_A = "A123456789"   # dob 2000-01-01：正常有卡批
PID_B = "B223456782"   # dob 2001-02-02：inbox 報告 auto
PID_C = "C100000003"   # dob 1990-03-03：無卡手動 pid 批
# --- 未建檔（首見證號）----------------------------------------------------
PID_F = "F131104093"   # 照片批首見證號
PID_D = "D100000004"   # inbox 報告首見證號

DATE = "2026-07-18"

# --- 預存 OCR 文字（fake_ocr 直接回傳檔案內容作為逐行文字）-----------------
LESION_TEXT = "皮膚病灶 紅疹 患部特寫\n無任何證件字樣"

CARD_A_TEXT = f"""全民健康保險
姓名 王小明
{PID_A}
出生年月日 2000-01-01"""

REPORT_B_CBC = f"""檢驗報告單
姓名 陳大文
{PID_B}
出生 2001-02-02
報告日期 2026-07-10
CBC 白血球 血紅素"""

REPORT_D_UNFILED = f"""檢驗報告
姓名 林小華
{PID_D}
出生 1995-05-05
報告日 2026-07-11
生化 AST ALT"""

PHOTO_F_FIRSTSEEN = f"""病灶照片
{PID_F}
無健保卡字樣"""


def fake_ocr_image_text(path_or_bytes, cfg):
    """測試替身：把「影像檔」的檔案文字內容當作 OCR 逐行結果回傳（免真引擎）。"""
    return Path(path_or_bytes).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 環境 fixture
# --------------------------------------------------------------------------
class Env:
    def __init__(self, cfg, conn, root):
        self.cfg = cfg
        self.conn = conn
        self.root = root

    def staging(self, relpath: str, text: str) -> Path:
        p = self.root / "staging" / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def inbox(self, name: str, text: str) -> Path:
        p = self.root / "inbox" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def archive_dir(self, key: str) -> Path:
        return self.root / "archive" / key

    def review_dir(self) -> Path:
        return self.root / "review"

    def records(self, **where):
        sql = "SELECT patient_key, taken_date, rtype, subtype, src, path, status, batch_key FROM records"
        clause = " AND ".join(f"{k}=?" for k in where)
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY id"
        return self.conn.execute(sql, tuple(where.values())).fetchall()

    def queue(self, **where):
        sql = "SELECT id, kind, reason, payload, state FROM queue_items"
        clause = " AND ".join(f"{k}=?" for k in where)
        if clause:
            sql += " WHERE " + clause
        sql += " ORDER BY id"
        return self.conn.execute(sql, tuple(where.values())).fetchall()

    def batch_state(self, key: str):
        return db.batch_state(self.conn, key)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "clinic_data"
    cfg = config.AppConfig(
        data_root=str(root),
        db_path=str(root / "clinic.db"),
        settle_seconds=0,   # 靜置窗設 0：檔案立即符合資格，不需 sleep
    )
    config.ensure_dirs(cfg)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.upsert_patient(conn, PID_A, name="王小明", dob="2000-01-01")
    db.upsert_patient(conn, PID_B, name="陳大文", dob="2001-02-02")
    db.upsert_patient(conn, PID_C, name="李阿姨", dob="1990-03-03")

    monkeypatch.setattr(ocr, "ocr_image_text", fake_ocr_image_text)
    yield Env(cfg, conn, root)
    conn.close()


# --------------------------------------------------------------------------
# staging：正常有卡批 → 自動歸檔
# --------------------------------------------------------------------------
def test_normal_card_batch_auto_files(env):
    key = f"{PID_A}|{DATE}|101500"
    env.staging(f"{PID_A}/{DATE}_101500_1.jpg", CARD_A_TEXT)   # N0：健保卡（首張）
    env.staging(f"{PID_A}/{DATE}_101500_2.jpg", LESION_TEXT)   # 病灶特寫

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts["auto"] == 1 and counts["queued"] == 0

    # 實體檔案：archive/A/ 有兩檔，皆歸為病灶照（卡片不再自動標識別影像）
    adir = env.archive_dir(PID_A)
    names = sorted(p.name for p in adir.iterdir())
    assert names == [f"{DATE}_病灶照_01.jpg", f"{DATE}_病灶照_02.jpg"]

    # DB records：兩筆、皆 病灶照/phone/auto、batch_key 一致
    rows = env.records(patient_key=PID_A)
    assert [r["rtype"] for r in rows] == ["病灶照", "病灶照"]
    assert all(r["src"] == "phone" and r["status"] == "auto" for r in rows)
    assert all(r["batch_key"] == key for r in rows)
    assert all(Path(r["path"]).exists() for r in rows)

    # 偵測到卡的圖掛 card_suspect 佇列（v0 不自動刪），payload 指向已歸檔的病灶照
    cards = env.queue(kind="card_suspect")
    assert len(cards) == 1
    import json

    payload = json.loads(cards[0]["payload"])
    assert payload["patient_key"] == PID_A
    assert payload["batch_key"] == key
    assert payload["files"][0].endswith("病灶照_01.jpg")   # 首張（N0 健保卡）
    assert Path(payload["files"][0]).exists()

    # processed_batches 記 auto_filed；staging 已清空、無殘留影像
    assert env.batch_state(key) == "auto_filed"
    assert list((env.root / "staging" / PID_A).glob("*.jpg")) == []

    # audit 有兩筆 auto_file
    n = env.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='auto_file' AND patient_key=?", (PID_A,)
    ).fetchone()[0]
    assert n == 2


# --------------------------------------------------------------------------
# staging：碰撞後綴混批 → 佇列（fail-closed，不誤併）
# --------------------------------------------------------------------------
def test_collision_suffix_batch_queues(env):
    key = f"{PID_A}|{DATE}|120000"
    env.staging(f"{PID_A}/{DATE}_120000_1.jpg", LESION_TEXT)
    env.staging(f"{PID_A}/{DATE}_120000_2.jpg", LESION_TEXT)
    env.staging(f"{PID_A}/{DATE}_120000_2-2.jpg", LESION_TEXT)   # 上游碰撞後綴

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1}

    items = env.queue(kind="photo_batch")
    assert len(items) == 1
    assert items[0]["reason"] == REASON_MIXED

    # 三檔全數搬入 review/（保留原名），staging 清空，無 records
    review_names = sorted(p.name for p in env.review_dir().iterdir())
    assert review_names == [
        f"{DATE}_120000_1.jpg",
        f"{DATE}_120000_2-2.jpg",
        f"{DATE}_120000_2.jpg",
    ]
    assert env.records() == []
    assert env.batch_state(key) == "queued"


# --------------------------------------------------------------------------
# staging：無卡手動 pid 批（pid 已建檔、複掃不到卡）→ 佇列
# --------------------------------------------------------------------------
def test_no_card_manual_pid_batch_queues(env):
    key = f"{PID_C}|{DATE}|131000"
    env.staging(f"{PID_C}/{DATE}_131000_1.jpg", LESION_TEXT)
    env.staging(f"{PID_C}/{DATE}_131000_2.jpg", LESION_TEXT)

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1}

    items = env.queue(kind="photo_batch")
    assert len(items) == 1
    assert items[0]["reason"] == "無卡批需人工一鍵確認"
    assert len(list(env.review_dir().iterdir())) == 2
    assert env.records() == []
    assert env.batch_state(key) == "queued"


# --------------------------------------------------------------------------
# staging：首見證號（有效但未建檔）→ 佇列
# --------------------------------------------------------------------------
def test_first_seen_id_photo_batch_queues(env):
    # _unsorted：無手機端 pid，OCR 複掃到有效但未建檔的證號
    env.staging(f"_unsorted/{DATE}_090000_1.jpg", PHOTO_F_FIRSTSEEN)

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1}

    items = env.queue(kind="photo_batch")
    assert len(items) == 1
    assert items[0]["reason"] == "首見證號"

    import json

    payload = json.loads(items[0]["payload"])
    assert PID_F in payload["extracted"]["ids"]   # 供 UI 顯示複掃到的證號
    assert env.records() == []
    assert env.batch_state(f"~|{DATE}|090000") == "queued"


# --------------------------------------------------------------------------
# inbox：報告 auto 與 queue 各一
# --------------------------------------------------------------------------
def test_inbox_report_auto_and_queue(env):
    env.inbox("report_b.jpg", REPORT_B_CBC)          # 已建檔＋生日吻合 → auto
    env.inbox("report_d.jpg", REPORT_D_UNFILED)      # 未建檔 → 首見證號 → queue

    counts = reports.process_inbox(env.conn, env.cfg)
    assert counts["auto"] == 1
    assert counts["queued"] == 1

    # auto：歸檔為 檢驗-CBC，taken_date 取報告日期，src=inbox
    adir = env.archive_dir(PID_B)
    filed = list(adir.iterdir())
    assert [p.name for p in filed] == ["2026-07-10_檢驗-CBC_01.jpg"]
    row = env.records(patient_key=PID_B)[0]
    assert (row["rtype"], row["subtype"], row["src"]) == ("檢驗", "CBC", "inbox")
    assert row["taken_date"] == "2026-07-10"

    # queue：report_d 進 review，佇列項 payload 帶抽出欄位供 UI 顯示
    items = env.queue(kind="report")
    assert len(items) == 1
    assert items[0]["reason"] == "首見證號"
    import json

    payload = json.loads(items[0]["payload"])
    assert PID_D in payload["extracted"]["ids"]
    assert payload["files"][0].endswith("report_d.jpg")
    assert Path(payload["files"][0]).exists()
    assert (env.root / "inbox" / "report_d.jpg").exists() is False


# --------------------------------------------------------------------------
# inbox：PDF 與非白名單副檔名 → 佇列（不做 OCR）
# --------------------------------------------------------------------------
def test_inbox_pdf_queues(env):
    env.inbox("scan.pdf", "%PDF-1.7 假裝的 PDF 位元組")

    counts = reports.process_inbox(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1, "skipped": 0}

    items = env.queue(kind="report")
    assert len(items) == 1
    assert items[0]["reason"] == "PDF 需人工"
    assert (env.review_dir() / "scan.pdf").exists()
    assert env.records() == []


def test_inbox_unsupported_ext_queues(env):
    env.inbox("note.txt", "這不是影像也不是報告")

    counts = reports.process_inbox(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1, "skipped": 0}

    items = env.queue(kind="report")
    assert items[0]["reason"] == "格式不支援"
    assert (env.review_dir() / "note.txt").exists()


# --------------------------------------------------------------------------
# staging：遲到檔（鍵已處理）→ 佇列 straggler
# --------------------------------------------------------------------------
def test_straggler_batch_queues(env):
    key = f"{PID_A}|{DATE}|101500"
    # 先跑一輪把正常有卡批自動歸檔並 mark auto_filed
    env.staging(f"{PID_A}/{DATE}_101500_1.jpg", CARD_A_TEXT)
    env.staging(f"{PID_A}/{DATE}_101500_2.jpg", LESION_TEXT)
    first = reports.process_staging(env.conn, env.cfg)
    assert first["auto"] == 1
    assert env.batch_state(key) == "auto_filed"

    # 同鍵的遲到檔落入 staging → 下一輪偵測為遲到檔並整組佇列
    env.staging(f"{PID_A}/{DATE}_101500_3.jpg", LESION_TEXT)
    second = reports.process_staging(env.conn, env.cfg)
    assert second == {"auto": 0, "queued": 1}

    items = env.queue(kind="straggler")
    assert len(items) == 1
    assert items[0]["reason"] == REASON_STRAGGLER
    assert (env.review_dir() / f"{DATE}_101500_3.jpg").exists()
    # 原自動歸檔的兩筆 records 不受影響
    assert len(env.records(patient_key=PID_A)) == 2


# --------------------------------------------------------------------------
# watcher.run_once：一輪同時處理 staging + inbox
# --------------------------------------------------------------------------
def test_watcher_run_once(env):
    env.staging(f"{PID_A}/{DATE}_101500_1.jpg", CARD_A_TEXT)
    env.staging(f"{PID_A}/{DATE}_101500_2.jpg", LESION_TEXT)
    env.inbox("report_b.jpg", REPORT_B_CBC)

    result = watcher.run_once(env.conn, env.cfg)
    assert result["staging"]["auto"] == 1
    assert result["inbox"]["auto"] == 1
    assert len(env.records(patient_key=PID_A)) == 2
    assert len(env.records(patient_key=PID_B)) == 1


# --------------------------------------------------------------------------
# watcher_loop：真執行緒（自開連線）處理後可優雅停止，跨連線可見
# --------------------------------------------------------------------------
def test_watcher_loop_thread_processes_and_stops(env):
    key = f"{PID_A}|{DATE}|101500"
    env.staging(f"{PID_A}/{DATE}_101500_1.jpg", CARD_A_TEXT)
    env.staging(f"{PID_A}/{DATE}_101500_2.jpg", LESION_TEXT)

    stop = threading.Event()
    t = threading.Thread(
        target=watcher.watcher_loop, args=(env.cfg, stop), kwargs={"poll_seconds": 0.02}
    )
    t.start()
    try:
        deadline = time.time() + 10.0
        # 以測試自身連線輪詢（整合注意 1：watcher 於另一執行緒用自己的連線寫入）
        while time.time() < deadline and env.batch_state(key) != "auto_filed":
            time.sleep(0.02)
    finally:
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive()
    assert env.batch_state(key) == "auto_filed"
    assert len(env.records(patient_key=PID_A)) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
