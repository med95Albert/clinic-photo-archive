"""純文字欄位抽取（SPEC §6 的 extract.py 部分）。

本模組**零 OCR 依賴、零自家模組相依**：只吃一段已辨識好的文字，抽出報告/卡片
上的結構化欄位。故意不 import taiwan_id、config 等，確保單元測試可獨立於其他工單
的進度執行。checksum 與全形正規化是 taiwan_id 的單一實作點，由下游 predicate 套用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# --- 資料結構 ---------------------------------------------------------------


@dataclass
class ReportFields:
    """從一段 OCR 文字抽出的欄位（純文字結果，不含任何判準/歸檔決策）。

    注意：`ids` 是「格式層候選」（[A-Z][0-9]{9}，逐行去雜訊、保序去重），
    **不做 checksum 過濾、不做全形正規化**——那是 taiwan_id 的單一實作點，
    由下游 predicate 於管線中套用。此舉讓 extract.py 零自家模組相依、可獨立測試。
    """

    ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    dob: str | None = None
    chart_no: str | None = None
    report_date: str | None = None
    keywords: set[str] = field(default_factory=set)


# --- 日期正規化 -------------------------------------------------------------

_ROC_EPOCH_OFFSET = 1911  # 民國年 + 1911 = 西元年

# 中文「年月日」格式（可含民國前綴、可有空白）
_CHINESE_DATE_RE = re.compile(
    r"(民國)?\s*(\d{1,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"
)
# 分隔符格式：- / .（年 1~4 位、月日 1~2 位）
_SEP_DATE_RE = re.compile(r"(\d{1,4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")


def _to_iso(year: int, month: int, day: int) -> str | None:
    """以 datetime.date 驗證合法性後回 ISO 字串；非法日期（2/30、13 月）回 None。"""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_date(s) -> str | None:
    """把多種日期寫法轉成 ISO（YYYY-MM-DD）；無法解析或非法日期回 None。

    支援：2019-05-04、2019/5/4、114/05/04、090.05.04、民國90年5月4日、
    民國114年5月4日、114-05-04 等。民國 vs 西元判定：
      - 有「民國」前綴 → 民國年（+1911）
      - 年份 4 位（>=1000）→ 西元年
      - 年份 <=3 位（<1000）→ 民國年（+1911）
    可傳入整行文字（會在其中搜尋日期樣式）。
    """
    if not s:
        return None
    text = str(s)

    m = _CHINESE_DATE_RE.search(text)
    if m:
        has_minguo = m.group(1) is not None
        year, month, day = int(m.group(2)), int(m.group(3)), int(m.group(4))
        if has_minguo or year < 1000:
            year += _ROC_EPOCH_OFFSET
        return _to_iso(year, month, day)

    m = _SEP_DATE_RE.search(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 1000:
            year += _ROC_EPOCH_OFFSET
        return _to_iso(year, month, day)

    return None


# --- 健保卡偵測 -------------------------------------------------------------


def detect_card(text) -> bool:
    """文字含「全民健康保險」或「健保卡」即視為卡片影像候選。"""
    if not text:
        return False
    return ("全民健康保險" in text) or ("健保卡" in text)


# --- 報告分類 ---------------------------------------------------------------

# 掃描用關鍵字詞庫（canonical 形；比對一律大小寫不敏感、子字串）。
# 子字串語意是刻意的：如「白血球」含「血球」、「檢驗報告單」含「檢驗/報告」。
REPORT_KEYWORDS: tuple[str, ...] = (
    "CBC",
    "血球",
    "血紅素",
    "生化",
    "AST",
    "ALT",
    "肌酸酐",
    "尿液",
    "IgE",
    "過敏原",
    "InBody",
    "體脂",
    "檢驗",
    "報告",
)


def _scan_keywords(text: str) -> set[str]:
    upper = text.upper()
    found: set[str] = set()
    for kw in REPORT_KEYWORDS:
        if kw.upper() in upper:
            found.add(kw)
    return found


def classify_report(keywords) -> tuple[str, str | None]:
    """依關鍵字集合決定 (rtype, subtype)；優先序即 SPEC §6 條列順序。"""
    kw = set(keywords)
    if kw & {"CBC", "血球", "血紅素"}:
        return ("檢驗", "CBC")
    if kw & {"生化", "AST", "ALT", "肌酸酐"}:
        return ("檢驗", "生化")
    if "尿液" in kw:
        return ("檢驗", "尿液")
    if kw & {"IgE", "過敏原"}:
        return ("檢驗", "過敏原")
    if kw & {"InBody", "體脂"}:
        return ("InBody", None)
    if kw & {"檢驗", "報告"}:
        return ("檢驗", None)
    return ("文件", None)


# --- 欄位抽取 ---------------------------------------------------------------

# 身分證/統一證號「格式層」候選：字首 A-Z + 9 位數字（checksum 交給 taiwan_id）
_ID_RE = re.compile(r"[A-Z][0-9]{9}")
# 姓名：SPEC §6 給定 [一-鿿]，即 CJK 統一表意文字 U+4E00–U+9FFF
_NAME_RE = re.compile(r"姓\s*名[:：]?\s*([一-鿿]{2,4})")
# 病歷號：病歷號 / 病歷號碼，後接英數（含 -）
_CHART_RE = re.compile(r"病歷號碼?[:：]?\s*([A-Za-z0-9-]+)")


def _extract_ids(text: str) -> list[str]:
    """逐行去雜訊後抽 [A-Z][0-9]{9} 候選；保序去重，避免跨行黏成假號。"""
    seen: set[str] = set()
    ids: list[str] = []
    for line in text.splitlines():
        normalized = re.sub(r"[^A-Z0-9]", "", line.upper())
        for match in _ID_RE.finditer(normalized):
            value = match.group(0)
            if value not in seen:
                seen.add(value)
                ids.append(value)
    return ids


def _extract_names(text: str) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for match in _NAME_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _extract_dob(text: str) -> str | None:
    for line in text.splitlines():
        if "出生" in line or "生日" in line:
            iso = normalize_date(line)
            if iso:
                return iso
    return None


def _extract_report_date(text: str) -> str | None:
    """優先「報告日」，退而求其次「採檢日」。"""
    report: str | None = None
    collected: str | None = None
    for line in text.splitlines():
        if "報告日" in line:
            iso = normalize_date(line)
            if iso and report is None:
                report = iso
        elif "採檢日" in line:
            iso = normalize_date(line)
            if iso and collected is None:
                collected = iso
    return report or collected


def _extract_chart_no(text: str) -> str | None:
    match = _CHART_RE.search(text)
    return match.group(1) if match else None


def extract_report_fields(text) -> ReportFields:
    """純文字欄位抽取入口：不呼叫任何 OCR 引擎、不 import 自家模組。"""
    text = text or ""
    return ReportFields(
        ids=_extract_ids(text),
        names=_extract_names(text),
        dob=_extract_dob(text),
        chart_no=_extract_chart_no(text),
        report_date=_extract_report_date(text),
        keywords=_scan_keywords(text),
    )
