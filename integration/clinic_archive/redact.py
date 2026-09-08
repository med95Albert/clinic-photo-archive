"""log／診斷輸出的病人識別碼遮罩（單一實作點）。

為什麼存在：部署 runbook 允許現場 agent 讀 ``integration.log`` 排錯，而 agent 的
對話會離開診所（送到模型供應商）。因此凡是會進 log、console、契約測試輸出的字串，
一律先經本模組遮罩——證號只留前 6 碼（與文件示意的 ``A12345****`` 慣例一致），
暫時代號留 ``P-`` 與前 3 碼。姓名不在此處處理：log 站點不得直接印姓名或
使用者取的檔名（inbox 檔名以 ``safe_name`` 改印雜湊）。

遮罩只用於**輸出**；DB、路徑、歸檔邏輯一律用完整值。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

# 前後不得緊鄰英數：避免把 "AB1234567890" 這類更長的編號切一段出來誤遮，
# 也避免對中文夾字（如「證號A123456789」）失效——CJK 不算英數，仍會命中。
_ID_RE = re.compile(r"(?<![A-Z0-9])([A-Z][0-9]{5})[0-9]{4}(?![0-9])")
_PCODE_RE = re.compile(r"(?<![A-Z0-9])(P-[0-9]{3})[0-9]{4}(?![0-9])")


def mask_text(text: str) -> str:
    """把字串中所有證號／暫時代號遮成 ``A12345****``／``P-123****``。"""
    if not text:
        return text
    return _PCODE_RE.sub(r"\1****", _ID_RE.sub(r"\1****", text))


def mask_pid(pid: str | None) -> str:
    """遮罩單一病患代碼；None 回空字串。"""
    return mask_text(pid or "")


def safe_name(path: str | Path) -> str:
    """把使用者取的檔名換成 ``<8 碼雜湊><副檔名>``（inbox 檔名可能含姓名）。"""
    p = Path(path)
    digest = hashlib.sha1(p.name.encode("utf-8")).hexdigest()[:8]
    return f"{digest}{p.suffix.lower()}"


class MaskingFormatter(logging.Formatter):
    """在最終格式化字串上遮罩——涵蓋訊息、參數與 traceback 內出現的識別碼。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return mask_text(super().format(record))
