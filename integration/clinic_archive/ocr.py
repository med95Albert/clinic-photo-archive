"""薄 OCR 引擎層（SPEC §6 的 ocr.py 部分）。

只負責「影像 → 逐行文字」。欄位抽取、身分證 checksum 等一律不在這裡做——
本模組**不 import 任何自家模組**（含 taiwan_id），SPEC §6 的 API 也不需要它。
rapidocr 採延遲 import，讓純文字流程（extract.py）在無 rapidocr 的環境仍可載入。
"""
from __future__ import annotations

import threading
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps


class OcrUnavailableError(RuntimeError):
    """OCR 後端不可用（rapidocr 未安裝，或初始化/解碼/推論失敗）。"""


# 全模組共用一把鎖：rapidocr / onnxruntime 的 session 不保證多執行緒安全，
# watcher 逐張序列化推論即可（v0 不追求並發吞吐）。
_OCR_LOCK = threading.Lock()


@lru_cache(maxsize=None)
def get_engine(ocr_version: str, det_side_len: int):
    """建立並快取 RapidOCR 引擎；(版本, 解析度) 各快取一個實例。

    rapidocr 3.9+ 每階段須同時指定 ocr_version 與 model_type：
    v6 用 medium（最準）、v5 用 mobile（一鍵退回）；免裝 paddlepaddle。
    延遲 import：僅在真的要跑 OCR 時才需要該依賴。
    """
    try:
        from rapidocr import ModelType, OCRVersion, RapidOCR
    except ImportError as exc:  # pragma: no cover - 取決於執行環境
        raise OcrUnavailableError("rapidocr 未安裝") from exc

    version_map = {
        "PPOCRV5": (OCRVersion.PPOCRV5, ModelType.MOBILE),
        "PPOCRV6": (OCRVersion.PPOCRV6, ModelType.MEDIUM),
    }
    version, model_type = version_map.get(
        str(ocr_version).upper(), (OCRVersion.PPOCRV6, ModelType.MEDIUM)
    )
    try:
        # limit_side_len 越大越準、越慢，由部署端調整。
        return RapidOCR(
            params={
                "Det.ocr_version": version,
                "Det.model_type": model_type,
                "Rec.ocr_version": version,
                "Rec.model_type": model_type,
                "Det.limit_side_len": det_side_len,
            }
        )
    except Exception as exc:  # pragma: no cover - 取決於執行環境
        raise OcrUnavailableError(f"RapidOCR 初始化失敗：{exc}") from exc


def _extract_texts(result) -> list[str]:
    """從 RapidOCR 回傳取出逐行文字，兼容新版 RapidOCROutput 與舊版 list。"""
    if result is None:
        return []
    texts = getattr(result, "txts", None)
    if texts is not None:
        return [str(t) for t in texts if t]
    lines: list[str] = []
    for item in result or []:
        if item and len(item) >= 2 and item[1]:
            lines.append(str(item[1]))
    return lines


def _load_image(path_or_bytes) -> Image.Image:
    """統一在此做 EXIF 方向校正並轉 RGB。

    rapidocr 只在「檔案路徑」輸入時做 EXIF 校正，直餵 bytes 會跳過；因此無論來源
    是 bytes 或路徑，都先自行 exif_transpose，保留手機直拍的方向語意後再餵引擎。
    """
    try:
        if isinstance(path_or_bytes, (bytes, bytearray)):
            image = Image.open(BytesIO(bytes(path_or_bytes)))
        else:
            image = Image.open(Path(path_or_bytes))
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")
    except Exception as exc:
        raise OcrUnavailableError(f"影像解碼失敗：{exc}") from exc


def ocr_image_text(path_or_bytes, cfg) -> str:
    r"""對單張影像（路徑或 bytes）做 OCR，回傳逐行文字（以 \n 相接）。

    cfg 需具備 .ocr_version 與 .det_side_len（duck typing，不 import config），
    以免與 T2 的 AppConfig 產生編譯期耦合。推論以模組鎖序列化。
    """
    engine = get_engine(cfg.ocr_version, cfg.det_side_len)
    image = _load_image(path_or_bytes)
    try:
        with _OCR_LOCK:
            result = engine(image)
    except Exception as exc:  # pragma: no cover - 取決於執行環境
        raise OcrUnavailableError(f"RapidOCR 推論失敗：{exc}") from exc
    return "\n".join(_extract_texts(result))


def ocr_pdf_text(path, cfg) -> str:
    """v0 不支援 PDF 文字抽取——刻意 fail-loud。

    依賴白名單無 pypdf/pdfplumber，掃描型 PDF 亦需額外 render→OCR 依賴；
    architecture/SPEC 的 v0 規則是「PDF 一律進佇列（reason='PDF 需人工'）」，
    由 process_inbox 在呼叫 OCR 前就攔截路由。這裡拒絕靜默回空字串，
    避免有人誤用而把 PDF 當成「無文字」放行（違反 fail-closed）。
    """
    raise NotImplementedError(
        "v0 不支援 PDF OCR；PDF 應由 process_inbox 直接進佇列（reason='PDF 需人工'）"
    )
