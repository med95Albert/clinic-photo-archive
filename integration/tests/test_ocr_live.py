"""OCR 端到端測試（需下載模型；預設以 addopts=-m 'not e2e' 跳過，CI 亦可跳）。

合成一張含「姓名／身分證字號／出生日期」的簡單影像，跑真實 ocr_image_text 後，
用 extract_report_fields 驗證欄位可被抽出——串起 ocr.py 與 extract.py 的實際路徑。
只有明確加上 `-m e2e`（或 `-m 'e2e'`）時才會執行。
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from clinic_archive.extract import extract_report_fields
from clinic_archive.ocr import ocr_image_text

pytestmark = pytest.mark.e2e

# 跨平台中文字型候選（同 bench/ocr_bench.py 慣例）
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    pytest.skip("找不到可用的中文字型，略過 OCR e2e")


def _make_card_like_image() -> bytes:
    """合成一張清晰、大字級的『姓名/身分證/出生日期』影像（非真實個資）。"""
    img = Image.new("RGB", (960, 480), "white")
    draw = ImageDraw.Draw(img)
    font = _load_font(40)
    draw.text((48, 60), "姓名：王小明", font=font, fill="black")
    draw.text((48, 180), "身分證字號：A123456789", font=font, fill="black")
    draw.text((48, 300), "出生日期：2019-05-04", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _cfg() -> SimpleNamespace:
    # duck-typed 設定物件：ocr_image_text 只取 .ocr_version 與 .det_side_len
    return SimpleNamespace(ocr_version="PPOCRV6", det_side_len=960)


def test_ocr_image_text_then_extract_fields():
    text = ocr_image_text(_make_card_like_image(), _cfg())
    assert text.strip(), "OCR 應回傳非空文字"

    fields = extract_report_fields(text)
    assert fields.names == ["王小明"], f"names={fields.names!r} text={text!r}"
    assert fields.dob == "2019-05-04", f"dob={fields.dob!r} text={text!r}"
    assert "A123456789" in fields.ids, f"ids={fields.ids!r} text={text!r}"


def test_ocr_accepts_path_input(tmp_path):
    path = tmp_path / "card.png"
    path.write_bytes(_make_card_like_image())
    text = ocr_image_text(path, _cfg())
    assert "王小明" in text.replace(" ", ""), f"text={text!r}"
