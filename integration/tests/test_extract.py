"""extract.py 密集單元測試（純文字、不需 OCR 引擎）。

涵蓋：normalize_date（含民國年/西元/中文年月日/非法日期）、detect_card、
classify_report 分類表逐條與優先序、姓名抽取、身分證候選（保序去重/不做 checksum/
不跨行黏字）、dob/report_date/chart_no 抽取、整份報告與健保卡整合。
"""
from __future__ import annotations

import pytest

from clinic_archive.extract import (
    ReportFields,
    classify_report,
    detect_card,
    extract_report_fields,
    normalize_date,
)

# --- normalize_date ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 西元，多種分隔符與位數
        ("2019-05-04", "2019-05-04"),
        ("2019/5/4", "2019-05-04"),
        ("2019/05/04", "2019-05-04"),
        ("2019.05.04", "2019-05-04"),
        ("2019年12月31日", "2019-12-31"),
        # 民國：3 位、含前導 0、含「民國」前綴、含空白、dash 分隔
        ("114/05/04", "2025-05-04"),
        ("090.05.04", "2001-05-04"),
        ("114-05-04", "2025-05-04"),
        ("民國90年5月4日", "2001-05-04"),
        ("民國114年5月4日", "2025-05-04"),
        ("民國 114 年 5 月 4 日", "2025-05-04"),
        ("90年5月4日", "2001-05-04"),
        # 整行文字（帶標籤）也能在其中搜尋出日期
        ("出生日期：2019-05-04", "2019-05-04"),
        ("報告日：2026-07-16", "2026-07-16"),
        ("出生年月日：民國108年5月4日", "2019-05-04"),
    ],
)
def test_normalize_date_ok(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "abc",
        "無日期資訊",
        "12345",  # 無分隔符、非年月日
        "2019-13-01",  # 月份非法
        "2019-02-30",  # 日期非法
        "2025-00-10",  # 月份 0
    ],
)
def test_normalize_date_bad(raw):
    assert normalize_date(raw) is None


def test_minguo_epoch_offset():
    # 民國年 = 西元 - 1911，逐一釘住幾個代表值
    assert normalize_date("民國1年1月1日") == "1912-01-01"
    assert normalize_date("108/01/01") == "2019-01-01"


# --- detect_card ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("全民健康保險", True),
        ("健保卡", True),
        ("這是全民健康保險 IC 卡", True),
        ("請持健保卡就醫", True),
        ("測試醫事檢驗所 檢驗報告單", False),
        ("健保", False),  # 部分字樣不算
        ("", False),
        (None, False),
    ],
)
def test_detect_card(text, expected):
    assert detect_card(text) is expected


# --- classify_report（分類表逐條 + 優先序）----------------------------------


@pytest.mark.parametrize(
    "keywords, expected",
    [
        # 逐條命中
        ({"CBC"}, ("檢驗", "CBC")),
        ({"血球"}, ("檢驗", "CBC")),
        ({"血紅素"}, ("檢驗", "CBC")),
        ({"生化"}, ("檢驗", "生化")),
        ({"AST"}, ("檢驗", "生化")),
        ({"ALT"}, ("檢驗", "生化")),
        ({"肌酸酐"}, ("檢驗", "生化")),
        ({"尿液"}, ("檢驗", "尿液")),
        ({"IgE"}, ("檢驗", "過敏原")),
        ({"過敏原"}, ("檢驗", "過敏原")),
        ({"InBody"}, ("InBody", None)),
        ({"體脂"}, ("InBody", None)),
        ({"檢驗"}, ("檢驗", None)),
        ({"報告"}, ("檢驗", None)),
        # 皆無 → 文件
        (set(), ("文件", None)),
        ({"隨機無關詞"}, ("文件", None)),
        # 優先序：CBC > 生化 > 尿液 > 過敏原 > InBody > 檢驗/報告
        ({"CBC", "生化", "尿液"}, ("檢驗", "CBC")),
        ({"生化", "尿液", "過敏原"}, ("檢驗", "生化")),
        ({"尿液", "過敏原", "InBody"}, ("檢驗", "尿液")),
        ({"過敏原", "InBody", "報告"}, ("檢驗", "過敏原")),
        ({"InBody", "檢驗", "報告"}, ("InBody", None)),
        ({"檢驗", "報告"}, ("檢驗", None)),
    ],
)
def test_classify_report(keywords, expected):
    assert classify_report(keywords) == expected


# --- 姓名抽取 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("姓名：王小明", ["王小明"]),
        ("姓 名：陳雅婷", ["陳雅婷"]),  # 姓/名之間空白
        ("姓名:林承翰", ["林承翰"]),  # 半形冒號
        ("姓名 黃郁涵", ["黃郁涵"]),  # 無冒號
        ("姓名：王明", ["王明"]),  # 2 字名
        ("姓名：歐陽子瑜", ["歐陽子瑜"]),  # 4 字名
        ("姓名：\n王小明", ["王小明"]),  # 名字在下一行（\s* 跨換行）
        ("姓名：王小明\n姓名：王小明", ["王小明"]),  # 去重
        ("姓名：王小明\n姓名：陳雅婷", ["王小明", "陳雅婷"]),  # 保序
        ("本頁沒有病患資料這行", []),  # 無「姓名」字樣
    ],
)
def test_name_extraction(text, expected):
    assert extract_report_fields(text).names == expected


def test_name_regex_is_spec_verbatim_known_limitation():
    # SPEC §6 給定的正則含「可選冒號」，故「姓名」後緊接 2~4 個中文字必被視為候選，
    # 即使那其實是「欄位」這類詞。這是規格取捨（OCR 常掉冒號），非本層負責過濾；
    # 誤抓由後續人工在佇列確認把關。此測試釘住此已知行為，避免無意間放寬/收緊。
    assert extract_report_fields("姓名欄位").names == ["欄位"]


# --- 身分證候選抽取 ---------------------------------------------------------


def test_ids_basic():
    assert extract_report_fields("身分證字號：A123456789").ids == ["A123456789"]


def test_ids_dedupe_and_order():
    text = "A123456789\nB234567890\nA123456789"
    assert extract_report_fields(text).ids == ["A123456789", "B234567890"]


def test_ids_strip_surrounding_noise():
    # 逐行去掉非英數雜訊（冒號、空白、全形標點）後仍可命中 ASCII 證號
    text = "身分證字號： A123456789 （備註）"
    assert "A123456789" in extract_report_fields(text).ids


def test_ids_are_format_level_not_checksum_filtered():
    # A123456788 格式符但 checksum 不過；extract 為候選層，必須照樣抽出，
    # 由下游 taiwan_id/predicate 才做 checksum 把關。
    assert extract_report_fields("身分證：A123456788").ids == ["A123456788"]


def test_ids_not_glued_across_lines():
    # 跨行數字不得黏成假號
    text = "A12345\n6789 0"
    assert extract_report_fields(text).ids == []


# --- dob / report_date / chart_no ------------------------------------------


def test_dob_extraction():
    assert extract_report_fields("出生日期：2019-05-04").dob == "2019-05-04"
    assert extract_report_fields("生日：民國90年5月4日").dob == "2001-05-04"
    assert extract_report_fields("出生：114/05/04").dob == "2025-05-04"
    assert extract_report_fields("此行無生日欄位").dob is None


def test_report_date_prefers_report_over_collection():
    text = "採檢日：2026-07-15\n報告日：2026-07-16"
    assert extract_report_fields(text).report_date == "2026-07-16"


def test_report_date_fallback_to_collection():
    assert extract_report_fields("採檢日：2026-07-15").report_date == "2026-07-15"


def test_report_date_none():
    assert extract_report_fields("完全沒有日期字樣").report_date is None


def test_chart_no_extraction():
    assert extract_report_fields("病歷號：12345").chart_no == "12345"
    assert extract_report_fields("病歷號碼：A0099").chart_no == "A0099"
    assert extract_report_fields("病歷號 88776").chart_no == "88776"
    # 有「病歷號」字樣但後面沒有英數 → 不誤抓
    assert extract_report_fields("病歷號欄位空白未填").chart_no is None
    assert extract_report_fields("本頁無此欄位").chart_no is None


# --- 整合：整份報告 / 健保卡 ------------------------------------------------


def test_extract_full_report():
    text = "\n".join(
        [
            "測試醫事檢驗所 檢驗報告單",
            "姓名：王小明",
            "身分證字號：A123456789",
            "出生日期：2019-05-04",
            "病歷號：12345",
            "採檢日：2026-07-15",
            "報告日：2026-07-16",
            "WBC 白血球 8.2",
            "HGB 血紅素 12.8",
        ]
    )
    f = extract_report_fields(text)
    assert f.ids == ["A123456789"]
    assert f.names == ["王小明"]
    assert f.dob == "2019-05-04"
    assert f.chart_no == "12345"
    assert f.report_date == "2026-07-16"  # 報告日優先於採檢日
    assert "血球" in f.keywords  # 由「白血球」子字串命中
    assert classify_report(f.keywords) == ("檢驗", "CBC")
    assert detect_card(text) is False


def test_extract_health_card():
    text = "\n".join(
        [
            "全民健康保險",
            "姓名：王小明",
            "身分證字號：A123456789",
            "出生年月日：民國108年5月4日",
        ]
    )
    assert detect_card(text) is True
    f = extract_report_fields(text)
    assert f.names == ["王小明"]
    assert f.ids == ["A123456789"]
    assert f.dob == "2019-05-04"  # 民國108 + 1911 = 2019


def test_extract_empty_text_defaults():
    f = extract_report_fields("")
    assert isinstance(f, ReportFields)
    assert f.ids == []
    assert f.names == []
    assert f.dob is None
    assert f.chart_no is None
    assert f.report_date is None
    assert f.keywords == set()
    assert classify_report(f.keywords) == ("文件", None)


def test_extract_none_text_is_safe():
    # extract_report_fields(None) 不應炸，視為空文字
    f = extract_report_fields(None)
    assert f.ids == [] and f.names == [] and f.dob is None
