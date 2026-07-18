"""SPEC.md 第 5 節（工單 T1）：clinic_archive/taiwan_id.py 的單元測試。

各條 checksum 範例皆先手算／獨立腳本覆核過才寫進來，不是憑印象亂編。
"""

from __future__ import annotations

import sqlite3

import pytest

from clinic_archive.taiwan_id import (
    LETTER_VALUES,
    classify_manual_input,
    extract_ids,
    extract_old_resident_ids,
    is_pcode,
    next_pcode,
    verify_checksum,
)

# ---------------------------------------------------------------------------
# LETTER_VALUES：內政部字母對照表
# ---------------------------------------------------------------------------


def test_letter_values_matches_official_table():
    # 逐字對照 SPEC 第 5 節與內政部標準；I/O 是跳號特例，W/X/Y 順序也不直覺，
    # 任何一個抄錯都會讓對應字首的所有身分證字號永遠 checksum 失敗。
    expected = {
        "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
        "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
        "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
        "Y": 31, "Z": 33,
    }
    assert LETTER_VALUES == expected
    assert len(LETTER_VALUES) == 26


# ---------------------------------------------------------------------------
# verify_checksum
# ---------------------------------------------------------------------------


def test_verify_checksum_known_valid_ids():
    # 手算覆核：A=10→[1,0]；weights=(1,9,8,7,6,5,4,3,2,1,1)
    # A123456789 → digits [1,0,1,2,3,4,5,6,7,8,9]，加權總和=130，130%10==0。
    assert verify_checksum("A123456789") is True
    # I/O 是官方跳號特例（I=34, O=35），各另手算一組覆核，避免只測到 A 字首
    # 沒抓到 LETTER_VALUES 表在特例欄位抄錯的情形。
    assert verify_checksum("I331509839") is True  # 手算總和 170，170%10==0
    assert verify_checksum("O834738299") is True  # 手算總和 250，250%10==0


def test_verify_checksum_known_invalid_ids():
    # 把上面驗證過的合法號末碼 9→0（weight=1 的位置，delta=-9）：
    # 總和 130-9=121，121%10=1，不整除。
    assert verify_checksum("A123456780") is False
    # 另一組獨立手算的無效號（非由合法號竄改而來）：總和 139，139%10=9。
    assert verify_checksum("B123456789") is False


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "A12345678",       # 少一位數字
        "A1234567890",     # 多一位數字
        "a123456789",      # 小寫字首：本函式嚴格比對，不做正規化
        "123456789A",      # 字首不是字母
        "AA23456789",      # 字首後接的不是純數字
        "A12345678X",      # 尾端混入字母
        None,              # 非字串輸入也要 fail-closed 回 False，不得丟例外
    ],
)
def test_verify_checksum_rejects_bad_format(bad):
    assert verify_checksum(bad) is False


def test_verify_checksum_single_digit_error_can_still_pass():
    """SPEC 要求文件化的「漏網例」：單一位數字打錯，checksum 仍然通過。

    checksum 只能偵測「加權總和 mod 10 != 0」的錯誤；當某位的權重與 10
    不互質（本例權重=5，gcd(5,10)=5），只要竄改的 delta 恰好是
    10/gcd(weight,10)=2 的倍數，加權後的總和 mod 10 不會改變。

    以下把合法號 A123456789 的第 5 碼（0-index 第 4 位、原值 '4'，
    此位權重為 5）改成 '6'（delta=+2）：
      原始 digits=[1,0,1,2,3,4,5,6,7,8,9] 總和=130
      竄改後 digits=[1,0,1,2,3,6,5,6,7,8,9] 總和=140
      130、140 都是 10 的倍數 → 兩者 checksum 都通過，但已是不同號碼。

    這條測試不是在證明實作有 bug，而是誠實記錄 checksum 演算法本身的
    偵錯能力上限：checksum 只能做「格式健檢」，不能取代其他佐證
    （predicate.py 的 auto_file 判準因此還要求生日或既有病歷比對）。
    """
    original = "A123456789"
    corrupted = "A123656789"  # 只改了第 5 碼：4 -> 6
    assert original != corrupted
    assert sum(1 for a, b in zip(original, corrupted) if a != b) == 1
    assert verify_checksum(original) is True
    assert verify_checksum(corrupted) is True

    # 第二組獨立例子（不同權重、不同位置），佐證這不是單一巧合：
    # 權重=2 的位置（pid[7]，原值 '7'）delta=-5：2*(-5)=-10 ≡ 0 (mod 10)。
    original2 = "A123456789"
    corrupted2 = "A123456289"  # 第 8 碼：7 -> 2
    assert sum(1 for a, b in zip(original2, corrupted2) if a != b) == 1
    assert verify_checksum(original2) is True
    assert verify_checksum(corrupted2) is True


# ---------------------------------------------------------------------------
# extract_ids
# ---------------------------------------------------------------------------


def test_extract_ids_basic_single_line():
    assert extract_ids("A123456789") == ["A123456789"]


def test_extract_ids_ignores_checksum_leaves_filtering_to_caller():
    # extract_ids 只做格式篩選，不驗 checksum——B123456789 格式符合但
    # checksum 不過（見上方 test_verify_checksum_known_invalid_ids），
    # 仍應被抽出，由呼叫端（predicate.py）自行套 verify_checksum 篩選。
    assert verify_checksum("B123456789") is False
    assert extract_ids("B123456789") == ["B123456789"]


def test_extract_ids_strips_noise_and_chinese_text():
    text = "姓名：王小明\n身分證：A123456789 先生\n地址：台北市"
    assert extract_ids(text) == ["A123456789"]


def test_extract_ids_handles_fullwidth_mixed_with_halfwidth():
    # 整行全形
    assert extract_ids("Ａ１２３４５６７８９") == ["A123456789"]
    # 同一行內全形、半形數字交雜（OCR 常見情形）
    assert extract_ids("身分證：A12345６789 先生") == ["A123456789"]


def test_extract_ids_does_not_glue_across_lines():
    # 若把整段文字接起來再掃，"A12345" + "6789" 會被誤黏成一個假的
    # A123456789；逐行掃描應該完全抓不到任何候選。
    text = "A12345\n6789"
    assert extract_ids(text) == []


def test_extract_ids_dedupes_and_preserves_order():
    text = "A123456789\n患者主訴：頭痛\nB812191361\n（重複）A123456789"
    assert extract_ids(text) == ["A123456789", "B812191361"]


def test_extract_ids_multiple_distinct_candidates_in_order():
    text = "I331509839\nO834738299\nA123456789"
    assert extract_ids(text) == ["I331509839", "O834738299", "A123456789"]


def test_extract_ids_empty_text():
    assert extract_ids("") == []


# ---------------------------------------------------------------------------
# extract_old_resident_ids
# ---------------------------------------------------------------------------


def test_extract_old_resident_ids_basic():
    assert extract_old_resident_ids("AB12345678") == ["AB12345678"]


def test_extract_old_resident_ids_no_checksum_filtering():
    # 舊式居留證號本來就沒有公開的 checksum 演算法可驗，格式符合即回傳。
    assert extract_old_resident_ids("ZZ99999999") == ["ZZ99999999"]


def test_extract_old_resident_ids_and_extract_ids_do_not_cross_match():
    # 舊式格式（兩碼字母+8碼數字）不會被 extract_ids 誤認成新式證號，反之亦然。
    assert extract_ids("AB12345678") == []
    assert extract_old_resident_ids("A123456789") == []


def test_extract_old_resident_ids_dedupes_and_preserves_order():
    text = "AB12345678\n備註\nCD87654321\nAB12345678"
    assert extract_old_resident_ids(text) == ["AB12345678", "CD87654321"]


# ---------------------------------------------------------------------------
# is_pcode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["P-0000001", "P-1234567", "P-9999999"],
)
def test_is_pcode_accepts_valid(key):
    assert is_pcode(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "",
        "P-123",          # 位數不足
        "P-12345678",     # 位數過多
        "p-0000001",      # 小寫：嚴格比對，不正規化
        "P0000001",       # 缺連字號
        "PP-0000001",     # 多一個字母
        "P-000000A",      # 尾端混入字母
        "A123456789",     # 身分證字號本身不是 pcode
        None,
    ],
)
def test_is_pcode_rejects_invalid(key):
    assert is_pcode(key) is False


# ---------------------------------------------------------------------------
# next_pcode（測試時自建最小 patients 表，見 SPEC 第 5 節）
# ---------------------------------------------------------------------------


@pytest.fixture
def patients_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE patients (patient_key TEXT PRIMARY KEY)")
    yield conn
    conn.close()


def _insert_keys(conn: sqlite3.Connection, keys: list[str]) -> None:
    conn.executemany(
        "INSERT INTO patients (patient_key) VALUES (?)", [(k,) for k in keys]
    )
    conn.commit()


def test_next_pcode_empty_table_starts_at_one(patients_conn):
    assert next_pcode(patients_conn) == "P-0000001"


def test_next_pcode_increments_from_max(patients_conn):
    _insert_keys(patients_conn, ["P-0000001", "P-0000003", "P-0000002"])
    assert next_pcode(patients_conn) == "P-0000004"


def test_next_pcode_does_not_backfill_gaps(patients_conn):
    # 只有 P-0000001 與 P-0000005：下一號是 0000006，不會拿去補 0000002。
    _insert_keys(patients_conn, ["P-0000001", "P-0000005"])
    assert next_pcode(patients_conn) == "P-0000006"


def test_next_pcode_ignores_non_pcode_and_malformed_rows(patients_conn):
    _insert_keys(
        patients_conn,
        [
            "P-0000005",
            "A123456789",   # 身分證字號病人，不是 pcode
            "P-ABCDEFG",    # 前綴符合但格式不對，SQL LIKE 會撈到、需靠 is_pcode 濾掉
            "p-0000099",    # SQLite LIKE 預設對 ASCII 大小寫不敏感，會被撈到；
                            # is_pcode 嚴格比對大寫，須被正確排除
        ],
    )
    assert next_pcode(patients_conn) == "P-0000006"


def test_next_pcode_works_with_row_factory_connection(patients_conn):
    # db.py 的 connect() 會設 row_factory=sqlite3.Row；next_pcode 必須兩種都相容。
    patients_conn.row_factory = sqlite3.Row
    _insert_keys(patients_conn, ["P-0000010"])
    assert next_pcode(patients_conn) == "P-0000011"


# ---------------------------------------------------------------------------
# classify_manual_input
# ---------------------------------------------------------------------------


def test_classify_manual_input_national_id():
    assert classify_manual_input("A123456789") == ("national_id", "A123456789")


def test_classify_manual_input_national_id_normalizes_case_and_whitespace_and_fullwidth():
    assert classify_manual_input("  a123456789  ") == ("national_id", "A123456789")
    assert classify_manual_input("Ａ１２３４５６７８９") == ("national_id", "A123456789")


def test_classify_manual_input_format_correct_but_checksum_fails_is_invalid():
    # 關鍵設計取捨：格式像身分證字號但 checksum 沒過，視為 invalid 而非
    # chart_no——這種字串多半是打錯的證號，fail-closed 不讓它靜默混進
    # 病歷號別名去比對（見 taiwan_id.py classify_manual_input 的 docstring）。
    assert verify_checksum("A123456780") is False
    assert classify_manual_input("A123456780") == ("invalid", "A123456780")


def test_classify_manual_input_checksum_fail_returns_normalized_value():
    # 「格式對但 checksum 沒過」分支回傳正規化後的 compact 形式（方便使用者
    # 比對輸入哪裡打錯），跟「完全無法辨識」分支回傳原始去頭尾空白字串不同。
    assert classify_manual_input("  a123456780  ") == ("invalid", "A123456780")


def test_classify_manual_input_pcode():
    assert classify_manual_input("P-0000123") == ("pcode", "P-0000123")


def test_classify_manual_input_pcode_normalizes_lowercase():
    # 注意：is_pcode() 本身嚴格比對大寫；classify_manual_input 在比對前
    # 先正規化，兩者的嚴格程度刻意不同（各自的 docstring 皆有說明）。
    assert classify_manual_input("p-0000123") == ("pcode", "P-0000123")
    assert is_pcode("p-0000123") is False


@pytest.mark.parametrize(
    "raw,expected_value",
    [
        ("0012345", "0012345"),
        ("CHT-0099", "CHT-0099"),
        ("cht0099", "CHT0099"),
        ("A1234", "A1234"),
    ],
)
def test_classify_manual_input_chart_no(raw, expected_value):
    assert classify_manual_input(raw) == ("chart_no", expected_value)


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "！！！", "----", "***"],
)
def test_classify_manual_input_invalid(raw):
    kind, _value = classify_manual_input(raw)
    assert kind == "invalid"


def test_classify_manual_input_invalid_preserves_stripped_original_value():
    kind, value = classify_manual_input("   ???   ")
    assert kind == "invalid"
    assert value == "???"
