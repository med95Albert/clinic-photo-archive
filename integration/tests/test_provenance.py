"""證據同源／病歷號唯一性／卡片統一處理的專項測試（Fix-A 缺陷回歸）。

用**真實** taiwan_id / db（非 stub），確保端到端行為真的成立：

  1. 跨圖混用攻擊被擋：N0 是 B 的卡（號讀不出、生日 D 讀得出），批內另一張文件照
     含 A 的有效證號，A 生日恰為 D → 舊碼會把卡的生日和文件的證號跨圖湊成假吻合而
     整批誤歸 A；新判準要求「證號與卡面生日同源」，此組合一律 queue('證據不同源需人工')。
  2. 同源卡通過：證號與卡面生日出自同一張健保卡且與建檔吻合 → auto。
  3. 病歷號重號被擋：同一 chart_no 對應多位病人 → queue('病歷號對應不唯一或不存在')。
  4. 單張純卡批也產 card_suspect：偵測到卡的圖一律歸『病灶照』，且不論批內張數（含單張
     純卡批）都建 card_suspect 佇列項供管理者裁決。

前三項在 predicate 層直接驗真值；第 1、4 項另經 process_staging 端到端驗證（含實體檔案與
DB 狀態），證明 reports.py 逐圖建 ImageEvidence 的接線正確。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinic_archive import config, db, ocr, predicate, reports
from clinic_archive.batching import BatchGroup
from clinic_archive.predicate import ImageEvidence, Verdict

# 真內政部 checksum 有效號（與 test_pipeline_e2e 同源，均為合法號）。
VALID_A = "A123456789"
VALID_B = "B223456782"
DATE = "2026-07-18"
# 攻擊者要湊的生日 D：A 的建檔生日恰為 D，且被讀在 B 的卡上。
DOB_D = "1988-08-08"


# ---------------------------------------------------------------------------
# predicate 層：逐圖同源真值
# ---------------------------------------------------------------------------
def pred_db():
    """真實 schema 的 in-memory 連線（Row factory，供 db.find_patients_by_chart 具名存取）。"""
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


def photo_group(pid, suspect=None):
    return BatchGroup(
        key=f"{pid or '~'}|{DATE}|101500", pid=pid,
        date=DATE, time="101500", files=[],
        complete=(suspect is None), suspect_reason=suspect,
    )


def test_cross_image_mixing_attack_blocked():
    """核心攻擊：卡的生日 D × 文件的證號 A 跨圖湊吻合 → 必須 fail-closed。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob=DOB_D)  # A 生日恰為 D
    g = photo_group(None)  # _unsorted：無手機端背書
    images = [
        # N0 是 B 的卡：ID 讀不出（無有效證號）、生日 D 讀得出
        ImageEvidence(path=Path("n0_card_of_B.jpg"), ids=[], is_card=True, dob=DOB_D),
        # 另一張文件照：含 A 的有效證號，非卡、無生日
        ImageEvidence(path=Path("doc_with_A.jpg"), ids=[VALID_A], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.patient_key is None          # 絕不歸給 A
    assert v.reason == "證據不同源需人工"


def test_cross_source_blocked_even_with_phone_endorsement():
    """即使手機端帶入代碼指向 A，只要卡上生日與證號不同源，仍不得自動歸檔。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob=DOB_D)
    g = photo_group(VALID_A)   # 手機端帶入 A
    images = [
        ImageEvidence(path=Path("other_card.jpg"), ids=[], is_card=True, dob=DOB_D),
        ImageEvidence(path=Path("doc_with_A.jpg"), ids=[VALID_A], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.reason == "證據不同源需人工"


def test_same_source_card_passes():
    """證號與卡面生日出自同一張卡且與建檔吻合 → auto。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)
    images = [
        # 同一張健保卡同時帶證號與生日＝同源
        ImageEvidence(path=Path("n0.jpg"), ids=[VALID_A], is_card=True, dob="2000-01-01"),
        # 另有一張病灶特寫（無證件資訊）
        ImageEvidence(path=Path("lesion.jpg"), ids=[], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v == Verdict(True, VALID_A, "證號已建檔＋卡面生日吻合")


def test_chart_duplicate_blocked():
    """同一 chart_no 對應兩位病人 → 病歷號解析不唯一，fail-closed。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01", chart_no="DUP-1")
    db.upsert_patient(conn, "P-0000007", name="另一人", dob="1999-09-09", chart_no="DUP-1")
    # 前提健全性：真的有兩筆同號
    assert len(db.find_patients_by_chart(conn, "DUP-1")) == 2

    g = photo_group("DUP-1")   # 手機端帶入重號病歷號
    images = [ImageEvidence(path=Path("n0.jpg"), ids=[VALID_A], is_card=True, dob="2000-01-01")]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.patient_key is None
    assert v.reason == "病歷號對應不唯一或不存在"


def test_chart_unique_still_resolves():
    """對照組：chart_no 恰一命中 → 正常解析並自動歸檔（證明擋的是重號，不是全擋）。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01", chart_no="UNIQ-9")
    g = photo_group("UNIQ-9")
    images = [ImageEvidence(path=Path("n0.jpg"), ids=[VALID_A], is_card=True, dob="2000-01-01")]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v == Verdict(True, VALID_A, "證號已建檔＋卡面生日吻合")


# ---------------------------------------------------------------------------
# pipeline 層：process_staging 端到端
# ---------------------------------------------------------------------------
# 預存 OCR 文字（fake_ocr 直接回傳檔案內容作為逐行文字）。
CARD_A_TEXT = f"""全民健康保險
姓名 王小明
{VALID_A}
出生年月日 2000-01-01"""

# B 的卡：偵測為卡、生日 D 讀得出，但證號讀不出（無任何 [A-Z][0-9]{{9}}）。
CARD_B_NOID_DOB_D = f"""全民健康保險
姓名 陳大文
出生年月日 {DOB_D}"""

# 文件照：含 A 的有效證號，非健保卡、無生日行。
DOC_WITH_A_ID = f"""轉診證明文件
證號 {VALID_A}
就診科別 皮膚科"""


def fake_ocr_image_text(path_or_bytes, cfg):
    return Path(path_or_bytes).read_text(encoding="utf-8")


class Env:
    def __init__(self, cfg, conn, root):
        self.cfg, self.conn, self.root = cfg, conn, root

    def staging(self, relpath: str, text: str) -> Path:
        p = self.root / "staging" / relpath
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
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    monkeypatch.setattr(ocr, "ocr_image_text", fake_ocr_image_text)
    yield Env(cfg, conn, root)
    conn.close()


def test_single_pure_card_batch_produces_card_suspect(env):
    """單張純卡批：偵測到卡 → 以病灶照歸檔＋建 card_suspect（不論張數）。"""
    key = f"{VALID_A}|{DATE}|101500"
    env.staging(f"{VALID_A}/{DATE}_101500_1.jpg", CARD_A_TEXT)   # 只有一張健保卡

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts == {"auto": 1, "queued": 0}

    # 卡片仍以病灶照歸檔（v0 不自動刪），檔名為病灶照
    adir = env.archive_dir(VALID_A)
    names = [p.name for p in adir.iterdir()]
    assert names == [f"{DATE}_病灶照_01.jpg"]
    rows = env.records(patient_key=VALID_A)
    assert len(rows) == 1 and rows[0]["rtype"] == "病灶照"

    # 單張批一樣掛 card_suspect，payload 指向歸檔後路徑與 record id
    cards = env.queue(kind="card_suspect")
    assert len(cards) == 1
    payload = json.loads(cards[0]["payload"])
    assert payload["patient_key"] == VALID_A
    assert payload["batch_key"] == key
    assert payload["files"][0].endswith("病灶照_01.jpg")
    assert Path(payload["files"][0]).exists()
    rid = env.conn.execute(
        "SELECT id FROM records WHERE path=?", (payload["files"][0],)
    ).fetchone()[0]
    assert payload["record_ids"] == [rid]
    assert env.batch_state(key) == "auto_filed"


def test_cross_source_attack_via_pipeline(env):
    """端到端：跨圖混用攻擊經 process_staging 一律進佇列，絕不歸檔到 A。"""
    # A 生日恰為攻擊者要湊的 D
    env.conn.execute("UPDATE patients SET dob=? WHERE patient_key=?", (DOB_D, VALID_A))
    env.conn.commit()

    # 同一上傳批（同時間戳、序號 1..2、無碰撞後綴）：B 的卡 + 含 A 證號的文件
    env.staging(f"_unsorted/{DATE}_140000_1.jpg", CARD_B_NOID_DOB_D)
    env.staging(f"_unsorted/{DATE}_140000_2.jpg", DOC_WITH_A_ID)

    counts = reports.process_staging(env.conn, env.cfg)
    assert counts == {"auto": 0, "queued": 1}

    items = env.queue(kind="photo_batch")
    assert len(items) == 1
    assert items[0]["reason"] == "證據不同源需人工"

    # 絕不歸檔到 A；兩張都進 review；批次標記 queued
    assert env.records() == []
    assert len(list(env.review_dir().iterdir())) == 2
    assert env.batch_state(f"~|{DATE}|140000") == "queued"

    # payload 仍帶複掃到的證號供 UI 顯示（資訊性）
    payload = json.loads(items[0]["payload"])
    assert VALID_A in payload["extracted"]["ids"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# 審查 R2 P0：手機背書分支的證據來源（證號必須出自卡片影像）
# ---------------------------------------------------------------------------


def test_endorsement_rejects_non_card_sourced_id():
    """卡讀不出任何資訊＋證號僅見於文件照 → 即使手機代碼一致也不得 auto（跨圖不同源）。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)  # 手機端帶入 A
    images = [
        ImageEvidence(path=Path("n0_unreadable_card.jpg"), ids=[], is_card=True, dob=None),
        ImageEvidence(path=Path("doc_with_A.jpg"), ids=[VALID_A], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.reason == "證據不同源需人工"


def test_no_card_at_all_doc_id_rejected():
    """批內完全無卡、證號只在文件照 → 不得以手機背書 auto（R2 P0 補強）。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)
    images = [
        ImageEvidence(path=Path("lesion.jpg"), ids=[], is_card=False, dob=None),
        ImageEvidence(path=Path("doc_with_A.jpg"), ids=[VALID_A], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.reason == "證號僅見於非卡片影像需人工"


def test_card_not_first_violates_n0_protocol():
    """錨點卡不在首張（病灶先拍、卡後拍）→ 一律人工（R3 P0：N0 首張邊界）。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)
    images = [
        ImageEvidence(path=Path("lesion_first.jpg"), ids=[], is_card=False, dob=None),
        ImageEvidence(path=Path("card_second.jpg"), ids=[VALID_A], is_card=True, dob="2000-01-01"),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.reason == "卡片非首張或多卡影像需人工"


def test_endorsement_accepts_card_sourced_id_with_unreadable_dob():
    """證號出自卡片（卡面生日讀不出）＋手機代碼一致 → 背書分支放行。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)
    images = [
        ImageEvidence(path=Path("n0_card.jpg"), ids=[VALID_A], is_card=True, dob=None),
        ImageEvidence(path=Path("lesion.jpg"), ids=[], is_card=False, dob=None),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is True
    assert v.patient_key == VALID_A


def test_later_second_card_blocks_even_after_first_card_matches():
    """A 卡首張全吻合＋批內後段另有一張卡（B 卡，號讀不出）→ 混批疑慮，一律人工（R4 P0）。"""
    conn = pred_db()
    db.upsert_patient(conn, VALID_A, name="王小明", dob="2000-01-01")
    g = photo_group(VALID_A)
    images = [
        ImageEvidence(path=Path("n0_card_A.jpg"), ids=[VALID_A], is_card=True, dob="2000-01-01"),
        ImageEvidence(path=Path("lesion.jpg"), ids=[], is_card=False, dob=None),
        ImageEvidence(path=Path("card_B_unreadable_id.jpg"), ids=[], is_card=True, dob=DOB_D),
    ]
    v = predicate.decide_photo_batch(conn, g, images)
    assert v.auto_file is False
    assert v.reason == "卡片非首張或多卡影像需人工"
