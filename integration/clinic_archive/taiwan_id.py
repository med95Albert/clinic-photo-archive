"""台灣身分證字號／統一證號工具。

提供字母對照表、checksum 驗證、OCR 文字逐行抽取、P- 流水號輔助，以及
人工輸入分類。僅實作 SPEC.md 第 5 節（工單 T1）之公開 API；本檔行為參考
ClinicSnap 的 ``services/patient_id_ocr.py``（唯讀參考，未複製整檔內容，
逐行抽取與 checksum 演算法為內政部公開標準，非該檔案的原創表達）。

fail-closed 提醒：本模組只負責「格式與 checksum 判斷」，不負責「這串號碼
真的對應到哪個病人」——那是呼叫端（predicate.py／webapp.py）的責任。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

__all__ = [
    "LETTER_VALUES",
    "verify_checksum",
    "extract_ids",
    "extract_old_resident_ids",
    "is_pcode",
    "next_pcode",
    "classify_manual_input",
]

# 內政部字母對照表：字首英文字母 → 兩位數值，供 checksum 加權使用。
# 注意 I=34、O=35 是官方定義的特例（不是接續 H=17 之後的 18、23），
# W=32／X=30／Y=31 的順序也不是字母序——這些都是標準本身的既有安排，
# 而非本實作的取捨，抄錯任一個都會讓所有合法證號被誤判為無效。
LETTER_VALUES: dict[str, int] = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}

# checksum 加權係數，依序套用在 [字母十位, 字母個位, 第1~9位數字] 共 11 碼。
_CHECKSUM_WEIGHTS = (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1)

# 新式統一證號／身分證字號：字首英文字母 + 9 位數字（兩者格式與 checksum 演算法相同，
# 2021 年後的新式統一證號沿用既有身分證字號格式，故毋須另開分支）。
_NATIONAL_ID_RE = re.compile(r"[A-Z][0-9]{9}")
# 舊式外來人口統一證號：兩位英文字母 + 8 位數字，官方未公開對應的 checksum 演算法。
_OLD_RESIDENT_ID_RE = re.compile(r"[A-Z]{2}[0-9]{8}")
# 系統配發的病人代碼（尚未取得證號時的暫用鍵）。
_PCODE_RE = re.compile(r"P-\d{7}")
# 病歷號別名：僅含大寫英數與連字號，且至少一個英數字元（避免純 "-" 之類的雜訊誤收）。
_CHART_NO_RE = re.compile(r"[A-Z0-9-]+")


def verify_checksum(pid: str) -> bool:
    """以內政部加權演算法驗證身分證字號／新式統一證號的檢查碼。

    嚴格比對 ``[A-Z][0-9]{9}``：不接受小寫、不自動轉全形為半形、不去除
    空白。正規化是呼叫端（``extract_ids``／``classify_manual_input``）的
    責任，本函式只做「格式已經乾淨」前提下的純數學驗證，職責單一。

    已知限制：checksum 只能偵測「加權後總和 mod 10 != 0」的錯誤。多個
    權重（5、8、6、4、2）與 10 不互質，代表某些單碼錯誤的 delta 剛好
    是 10/gcd(weight,10) 的倍數時，checksum 依然會通過——checksum 是
    「格式健檢」而非「身分證明」，見 tests 中的漏網對照組。
    """
    if not _NATIONAL_ID_RE.fullmatch(pid or ""):
        return False
    letter_value = LETTER_VALUES[pid[0]]
    digits = [letter_value // 10, letter_value % 10] + [int(c) for c in pid[1:]]
    total = sum(d * w for d, w in zip(digits, _CHECKSUM_WEIGHTS, strict=True))
    return total % 10 == 0


def _clean_line(line: str) -> str:
    """單行正規化：NFKC（全形→半形）→ 轉大寫 → 去除英數以外的雜訊字元。"""
    normalized = unicodedata.normalize("NFKC", line).upper()
    return re.sub(r"[^A-Z0-9]", "", normalized)


def _scan_lines(text: str, pattern: re.Pattern[str]) -> list[str]:
    """逐行掃描候選、去重、保序回傳。

    刻意「逐行」而非把整段文字接成一坨再掃描：OCR 輸出常常一行一個欄位，
    若跨行接字，換行前後兩串各自不完整的數字可能被誤黏成一組假的證號。
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        cleaned = _clean_line(line)
        for match in pattern.finditer(cleaned):
            value = match.group(0)
            if value not in seen:
                seen.add(value)
                out.append(value)
    return out


def extract_ids(text: str) -> list[str]:
    """逐行抽取身分證字號／新式統一證號候選。

    只做格式篩選（``[A-Z][0-9]{9}``），刻意不在此過濾 checksum——是否
    通過 checksum 交給呼叫端決定（predicate.py 會對這份候選清單套
    ``verify_checksum`` 再挑出「恰一」合法候選），避免這裡的策略選擇
    綁死下游判準。
    """
    return _scan_lines(text, _NATIONAL_ID_RE)


def extract_old_resident_ids(text: str) -> list[str]:
    """逐行抽取舊式外來人口統一證號候選（無 checksum 可驗，僅格式篩選）。"""
    return _scan_lines(text, _OLD_RESIDENT_ID_RE)


def is_pcode(key: str) -> bool:
    """是否為系統配發的 P- 流水號（``^P-\\d{7}$``）。

    嚴格比對，不做大小寫或全半形正規化——那是 ``classify_manual_input``
    在呼叫本函式之前的責任，這裡維持與 SPEC 給的正規表示式逐字一致。
    """
    return bool(_PCODE_RE.fullmatch(key or ""))


def next_pcode(conn: sqlite3.Connection) -> str:
    """查詢 patients 表既有的 P- 流水號，回傳下一號（``P-0000001`` 起算）。

    純「目前最大值 + 1」，不回填空號（例如刪掉 P-0000002 後不會被重用）；
    這樣即使有病人被合併／改鍵，P- 號碼也不會被誤配給另一位病人。
    """
    cur = conn.execute(
        "SELECT patient_key FROM patients WHERE patient_key LIKE ?", ("P-%",)
    )
    max_seq = 0
    for row in cur.fetchall():
        key = row[0]
        if is_pcode(key):
            seq = int(key[2:])
            if seq > max_seq:
                max_seq = seq
    return f"P-{max_seq + 1:07d}"


def classify_manual_input(s: str) -> tuple[str, str]:
    """分類人工輸入字串，回傳 ``(kind, value)``。

    正規化順序：去頭尾空白 → NFKC（全形轉半形）→ 去內部空白 → 轉大寫；
    ``value`` 一律回傳正規化後的結果（``chart_no`` 亦同，換取跟既有
    紀錄比對時的一致性；若 HIS 病歷號本身大小寫有意義，這是已知取捨）。

    kind 對應規則：
      - ``national_id``：符合 ``[A-Z][0-9]{9}`` 且 checksum 通過。
      - ``pcode``      ：符合 ``^P-\\d{7}$``。
      - ``chart_no``   ：其餘非空英數（可含 ``-``）字串，視為 HIS 病歷號別名。
      - ``invalid``    ：空字串／純符號；或格式長得像身分證但 checksum
        沒過——這種字串八成是打錯的證號而不是病歷號，fail-closed 原則
        下寧可回報 invalid 讓人工重新確認，也不要靜默收作 chart_no
        （否則可能查無此病歷號、或更糟——巧合命中別的病人）。
    """
    stripped = (s or "").strip()
    if not stripped:
        return ("invalid", stripped)

    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", stripped)).upper()
    if not compact:
        return ("invalid", stripped)

    if is_pcode(compact):
        return ("pcode", compact)

    if _NATIONAL_ID_RE.fullmatch(compact):
        if verify_checksum(compact):
            return ("national_id", compact)
        return ("invalid", compact)

    if _CHART_NO_RE.fullmatch(compact) and any(ch.isalnum() for ch in compact):
        return ("chart_no", compact)

    return ("invalid", stripped)
