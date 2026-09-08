"""log／診斷輸出的病人識別資料淨化（單一實作點）。

為什麼存在：部署 runbook 允許現場 agent 讀 log 排錯，而 agent 的對話會離開診所
（送到模型供應商）。因此凡是會進 log、console、契約測試輸出的字串，一律先經本模組
淨化。三層規則，全部是**機械**規則、不靠人眼：

1. 識別碼：證號只留前 6 碼（``A12345****``，與文件示意一致）、暫時代號留 ``P-123****``。
2. 路徑：``inbox/ review/ trash/ staging/ archive/`` 之後的每一個路徑段，只有「系統自己
   產生的名字」（``YYYY-MM-DD_HHMMSS_n[-k].ext``、日期資料夾、``_unsorted``、已遮罩的
   識別碼）原樣保留，其餘（使用者取的檔名——LINE 下載檔名、含姓名的檔名）一律換成
   ``h<8 碼雜湊><副檔名>``。traceback 裡的路徑同樣適用。
3. 網址 query：``?q=王小明`` 這類值一律換成 ``<redacted>``（病人搜尋字串就是姓名）。

遮罩只用於**輸出**；DB、路徑、歸檔邏輯一律用完整值。
也可當 CLI 用來讀任何 log（含上游 ClinicSnap 的 log）：
    python -m clinic_archive.redact <檔案> [--tail N]
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from pathlib import Path

# 前後不得緊鄰英數：避免把 "AB1234567890" 這類更長的編號切一段出來誤遮，
# 也避免對中文夾字（如「證號A123456789」）失效——CJK 不算英數，仍會命中。
_ID_RE = re.compile(r"(?<![A-Z0-9])([A-Z][0-9]{5})[0-9]{4}(?![0-9])")
_PCODE_RE = re.compile(r"(?<![A-Z0-9])(P-[0-9]{3})[0-9]{4}(?![0-9])")

# 工作資料夾名之後的連續路徑段（POSIX 與 Windows 分隔符皆可）。
_WORKDIRS = ("inbox", "review", "trash", "staging", "archive")
_PATH_RE = re.compile(
    r"(?P<dir>(?<![\w])(?:" + "|".join(_WORKDIRS) + r")(?![\w]))"
    r"(?P<rest>(?:[\\/][^\\/\s'\"<>|:?,;)\]]+)+)"
)
# 系統自己產生、不含個資的路徑段：保留原樣。
_SAFE_SEGMENT_RES = (
    re.compile(r"^\d{4}-\d{2}-\d{2}[\w.\-]*$"),      # 2026-07-18_101530_1-2.jpg、日期資料夾
    re.compile(r"^_unsorted$"),
    re.compile(r"^[A-Z][0-9]{5}\*{4}$"),              # 已遮罩證號
    re.compile(r"^P-[0-9]{3}\*{4}$"),                 # 已遮罩暫時代號
    re.compile(r"^\.partial$"),
)
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_QUERY_RE = re.compile(r"([?&])([A-Za-z_][\w\-.]*)=([^&\s\"'<>]*)")


def _hash_name(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    m = _EXT_RE.search(name)
    return f"h{digest}{m.group(0).lower() if m else ''}"


def mask_segment(segment: str) -> str:
    """單一路徑段／檔名：識別碼遮罩後，非系統產生的名字換成雜湊。"""
    seg = _PCODE_RE.sub(r"\1****", _ID_RE.sub(r"\1****", segment))
    if any(r.match(seg) for r in _SAFE_SEGMENT_RES):
        return seg
    return _hash_name(segment)


def _mask_path(m: re.Match) -> str:
    rest = m.group("rest")
    out = []
    for piece in re.split(r"([\\/])", rest):
        out.append(piece if piece in ("/", "\\", "") else mask_segment(piece))
    return m.group("dir") + "".join(out)


def mask_text(text: str) -> str:
    """遮罩識別碼 → 淨化工作資料夾下的路徑段 → 遮 query 值。"""
    if not text:
        return text
    text = _PCODE_RE.sub(r"\1****", _ID_RE.sub(r"\1****", text))
    text = _PATH_RE.sub(_mask_path, text)
    return _QUERY_RE.sub(r"\1\2=<redacted>", text)


def mask_pid(pid: str | None) -> str:
    """遮罩單一病患代碼；None 回空字串。"""
    return mask_text(pid or "")


def safe_name(path: str | Path) -> str:
    """把使用者取的檔名換成 ``h<8 碼雜湊><副檔名>``（inbox 檔名可能含姓名）。"""
    return _hash_name(Path(path).name)


class MaskingFormatter(logging.Formatter):
    """在最終格式化字串上淨化——涵蓋訊息、參數與 traceback 內出現的識別碼與路徑。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return mask_text(super().format(record))


def sanitize_lines(lines: list[str]) -> list[str]:
    return [mask_text(line) for line in lines]


def main(argv: list[str] | None = None) -> int:
    """淨化後印出一個 log 檔（現場 agent 讀任何 log 的唯一合法通道）。"""
    p = argparse.ArgumentParser(description="淨化後印出 log（遮證號、雜湊工作資料夾下檔名、遮 query 值）")
    p.add_argument("path", help="log 檔路徑（integration.log、clinic_snap.log 或任何文字檔）")
    p.add_argument("--tail", type=int, default=200, help="只印最後 N 行（預設 200；0＝全部）")
    args = p.parse_args(argv)
    try:
        raw = Path(args.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"讀取失敗：{exc}", file=sys.stderr)
        return 2
    lines = raw.splitlines()
    if args.tail and args.tail > 0:
        lines = lines[-args.tail:]
    # 明確重設 stdout 編碼：Windows 主控台 cp950 遇到 log 內的非 BMP 字元不該 crash。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass
    for line in sanitize_lines(lines):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
