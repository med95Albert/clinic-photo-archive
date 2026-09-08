"""判準閘門真值表測試（architecture §4 / SPEC §8）。

依賴的 T1(taiwan_id) 以 sys.modules 注入**最小但忠實**的 stub（真的內政部 checksum），
令測試 hermetic、可即時全綠，且透過 monkeypatch.setitem 自動還原，不污染其他測試模組。
病歷號查詢走 predicate → db.find_patients_by_chart（真實 db 模組對測試自建的 in-memory
連線執行 SQL），故 make_db 設 Row factory 讓 rows[i]["patient_key"] 可用。

照片批判準採**逐圖證據**（ImageEvidence）：每張圖各自帶 ids / is_card / dob，判準要求
「證號與卡面生日同源」——不再把全批證號與生日匯總後湊吻合（跨圖混用＝歸錯人）。

特別涵蓋 SPEC §8 指名的三個 fail-closed 例：
  * 有效證號但未建檔 → 首見證號
  * 證號吻合但生日不符 → 生日不符
  * 無卡但 pid=已建檔 → 無卡批需人工一鍵確認
（跨圖不同源攻擊、病歷號重號、單張純卡批另見 test_provenance.py。）
"""

import re
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from clinic_archive.batching import BatchGroup

# --- SPEC §3 DDL（測試自建 in-memory sqlite 並執行）------------------------
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
"""

# --- 忠實的 taiwan_id stub（內政部加權 checksum）---------------------------
_LETTER = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}


def _verify_checksum(pid: str) -> bool:
    if not isinstance(pid, str) or not re.fullmatch(r"[A-Z][0-9]{9}", pid):
        return False
    n = _LETTER[pid[0]]
    digits = [n // 10, n % 10] + [int(c) for c in pid[1:]]
    weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    return sum(d * w for d, w in zip(digits, weights)) % 10 == 0


def _classify_manual_input(s):
    s = (s or "").strip()
    if re.fullmatch(r"[A-Z][0-9]{9}", s) and _verify_checksum(s):
        return ("national_id", s)
    if re.fullmatch(r"P-\d{7}", s):
        return ("pcode", s)
    if s.isalnum():
        return ("chart_no", s)
    return ("invalid", s)


@pytest.fixture(autouse=True)
def _inject_taiwan_id(monkeypatch):
    stub = types.ModuleType("clinic_archive.taiwan_id")
    stub.verify_checksum = _verify_checksum
    stub.classify_manual_input = _classify_manual_input
    monkeypatch.setitem(sys.modules, "clinic_archive.taiwan_id", stub)
    yield


# import 放在 fixture 之後仍安全：predicate 以 importlib 於每次呼叫取用 sys.modules
from clinic_archive import predicate  # noqa: E402
from clinic_archive.predicate import ImageEvidence, Verdict  # noqa: E402

# 已驗證的有效證號（見 stub checksum）
VALID_A = "A123456789"   # sum 130
VALID_B = "B223456782"   # sum 140
VALID_C = "F131104093"   # 另一個有效號（測多證號用）


@dataclass
class RF:
    """extract.ReportFields 的最小替身（predicate 只讀 ids/dob）。"""
    ids: list
    dob: str | None = None
    names: list = field(default_factory=list)
    chart_no: str | None = None
    report_date: str | None = None
    keywords: set = field(default_factory=set)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row   # predicate → db.find_patients_by_chart 用具名欄位存取
    conn.executescript(DDL)
    return conn


def add_patient(conn, key, dob=None, chart_no=None, name="王小明"):
    conn.execute(
        "INSERT INTO patients(patient_key, name, dob, chart_no) VALUES(?,?,?,?)",
        (key, name, dob, chart_no),
    )
    conn.commit()


def photo_group(pid, suspect=None):
    return BatchGroup(
        key=f"{pid or '~'}|2026-07-18|101500", pid=pid,
        date="2026-07-18", time="101500", files=[],
        complete=(suspect is None), suspect_reason=suspect,
    )


def img(ids=(), is_card=False, dob=None, name="p.jpg"):
    """建一張圖的 ImageEvidence（逐圖證據）。"""
    return ImageEvidence(path=Path(name), ids=list(ids), is_card=is_card, dob=dob)


def card(ids=(), dob=None, name="card.jpg"):
    """健保卡影像（is_card=True）；證號與卡面生日皆屬同一張＝同源。"""
    return img(ids=ids, is_card=True, dob=dob, name=name)


def lesion(ids=(), name="lesion.jpg"):
    """病灶／文件影像（非卡）。"""
    return img(ids=ids, is_card=False, name=name)


# ---- 前置健全性：所用測試號確實有效 / 無效 -------------------------------
def test_stub_checksum_sanity():
    assert _verify_checksum(VALID_A)
    assert _verify_checksum(VALID_B)
    assert _verify_checksum(VALID_C)
    assert not _verify_checksum("A123456788")  # 末碼錯


# ---- 照片批：可疑批直接佇列 ----------------------------------------------
def test_photo_suspect_reason_queues():
    conn = make_db()
    g = photo_group(VALID_A, suspect="序號不連續或碰撞後綴＝疑混批")
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v == Verdict(False, None, "序號不連續或碰撞後綴＝疑混批")


# ---- 照片批：pid 解析失敗/查無 ------------------------------------------
def test_photo_pid_invalid_queues():
    conn = make_db()
    g = photo_group("!!亂碼!!")
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v.auto_file is False
    assert v.reason == "手機端身份代碼無法解析"


def test_photo_pcode_not_exist_queues():
    conn = make_db()
    g = photo_group("P-0000009")   # 未建檔的暫時代號
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v.auto_file is False
    assert v.reason == "暫時代號查無此人"


def test_photo_chartno_not_found_queues():
    conn = make_db()
    g = photo_group("CH5566")      # 病歷號查無 → 統一 reason
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v.auto_file is False
    assert v.reason == "病歷號對應不唯一或不存在"


# ---- 照片批：恰一原則 ----------------------------------------------------
def test_photo_multiple_ids_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    add_patient(conn, VALID_B, dob="2001-02-02")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(
        conn, g, [card(ids=[VALID_A, VALID_B], dob="2000-01-01")]
    )
    assert v.auto_file is False
    assert v.reason == "批內多個證號"


def test_photo_valid_plus_checksum_invalid_id_queues():
    # 跨模型審查 2026-09-09：一個有效 + 一個 checksum 不過的候選，過去會先濾掉
    # 無效者再數 → 「恰一」→ 自動歸檔。但那個無效候選可能是**第二位病人**被誤讀
    # 一碼的證號（兩人同框／兩人報告）。恰一原則在格式層計數：>1 → 佇列。
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(
        conn, g, [card(ids=[VALID_A, "A123456788"], dob="2000-01-01")]
    )
    assert v.auto_file is False
    assert v.reason == "批內多個證號"


def test_photo_invalid_ids_across_images_still_count():
    # 無效候選出現在另一張（非卡）圖上也要算——恰一是全批格式層計數。
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(
        conn, g,
        [card(ids=[VALID_A], dob="2000-01-01"), lesion(ids=["B123456781"])],
    )
    assert v.auto_file is False
    assert v.reason == "批內多個證號"


def test_photo_single_checksum_invalid_id_queues():
    # 恰一但檢查碼不過 → 佇列（不是「無任何身份線索」，讓人工看得出是誤讀）。
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(
        conn, g, [card(ids=["A123456788"], dob="2000-01-01")]
    )
    assert v.auto_file is False
    assert v.reason == "證號未通過檢查碼"


# ---- KEY fail-closed #1：有效證號但未建檔 → 首見證號 ----------------------
def test_photo_valid_but_unfiled_is_first_seen():
    conn = make_db()
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v.auto_file is False
    assert v.reason == "首見證號"


# ---- 照片批：卡面生日可讀且同源 ------------------------------------------
def test_photo_card_dob_match_auto():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v == Verdict(True, VALID_A, "證號已建檔＋卡面生日吻合")


# ---- KEY fail-closed #2：證號吻合但生日不符 → 生日不符 --------------------
def test_photo_card_dob_mismatch_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="1999-12-31")])
    assert v.auto_file is False
    assert v.reason == "生日不符"


def test_photo_filed_without_dob_but_card_readable_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob=None)   # 建檔缺生日
    g = photo_group(VALID_A)
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v.auto_file is False
    assert v.reason == "建檔資料缺生日，無法交叉核對"


# ---- 照片批：卡面生日不可讀 → 靠手機端人工背書 ---------------------------
def test_photo_no_card_dob_endorsement_match_auto():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)   # 手機端帶入代碼 == 複掃證號；卡面生日讀不出
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob=None)])
    assert v == Verdict(True, VALID_A, "證號已建檔＋手機端人工背書一致")


def test_photo_no_card_dob_endorsement_mismatch_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    add_patient(conn, VALID_B, dob="2001-02-02")
    g = photo_group(VALID_A)   # 手機端帶 A，但複掃證號是 B
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_B], dob=None)])
    assert v.auto_file is False
    assert v.reason == "卡面生日不可讀且手機端代碼未背書"


def test_photo_no_card_dob_pid_none_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(None)      # _unsorted：無手機端背書
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob=None)])
    assert v.auto_file is False
    assert v.reason == "卡面生日不可讀且手機端代碼未背書"


def test_photo_chartno_alias_resolves_and_auto():
    # pid 是病歷號，恰一映射到已建檔病人；同源卡生日吻合 → auto，patient_key 為證號
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01", chart_no="CH5566")
    g = photo_group("CH5566")
    v = predicate.decide_photo_batch(conn, g, [card(ids=[VALID_A], dob="2000-01-01")])
    assert v == Verdict(True, VALID_A, "證號已建檔＋卡面生日吻合")


# ---- KEY fail-closed #3：無卡批 + pid=已建檔 → 需人工一鍵確認 -------------
def test_photo_no_card_pid_filed_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    g = photo_group(VALID_A)   # pid 已建檔，但整批無任何證號被複掃出
    v = predicate.decide_photo_batch(conn, g, [lesion(ids=[])])
    assert v.auto_file is False
    assert v.reason == "無卡批需人工一鍵確認"


def test_photo_no_card_no_pid_no_clue_queues():
    conn = make_db()
    g = photo_group(None)      # 無卡、無 pid
    v = predicate.decide_photo_batch(conn, g, [lesion(ids=[])])
    assert v.auto_file is False
    assert v.reason == "無任何身份線索"


# ---- 報告分支 ------------------------------------------------------------
def test_report_zero_ids_queues():
    conn = make_db()
    v = predicate.decide_report(conn, RF(ids=[], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "報告無有效證號"


def test_report_multiple_ids_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    add_patient(conn, VALID_B, dob="2001-02-02")
    v = predicate.decide_report(conn, RF(ids=[VALID_A, VALID_B], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "報告內多個證號"


def test_report_valid_but_unfiled_first_seen():
    conn = make_db()
    v = predicate.decide_report(conn, RF(ids=[VALID_A], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "首見證號"


def test_report_missing_dob_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    v = predicate.decide_report(conn, RF(ids=[VALID_A], dob=None))
    assert v.auto_file is False
    assert v.reason == "報告缺生日"


def test_report_dob_mismatch_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    v = predicate.decide_report(conn, RF(ids=[VALID_A], dob="1999-12-31"))
    assert v.auto_file is False
    assert v.reason == "生日不符"


def test_report_filed_without_dob_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob=None)
    v = predicate.decide_report(conn, RF(ids=[VALID_A], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "建檔資料缺生日，無法交叉核對"


def test_report_dob_match_auto():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    v = predicate.decide_report(conn, RF(ids=[VALID_A], dob="2000-01-01"))
    assert v == Verdict(True, VALID_A, "報告證號已建檔＋生日吻合")


def test_report_valid_plus_checksum_invalid_id_queues():
    # 同 test_photo_valid_plus_checksum_invalid_id_queues：報告分支也在格式層計數。
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    v = predicate.decide_report(conn, RF(ids=["A123456788", VALID_A], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "報告內多個證號"


def test_report_single_checksum_invalid_id_queues():
    conn = make_db()
    add_patient(conn, VALID_A, dob="2000-01-01")
    v = predicate.decide_report(conn, RF(ids=["A123456788"], dob="2000-01-01"))
    assert v.auto_file is False
    assert v.reason == "報告證號未通過檢查碼"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
