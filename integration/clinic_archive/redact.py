"""log／診斷輸出的病人識別資料淨化（單一實作點）。

為什麼存在：部署 runbook 允許現場 agent 讀 log 排錯，而 agent 的對話會離開診所
（送到模型供應商）。因此凡是會進 log、console、契約測試輸出的字串，一律先經本模組
淨化。三層規則，全部是**機械**規則、不靠人眼：

1. 識別碼：證號只留前 6 碼（``A12345****``，與文件示意一致）、暫時代號留 ``P-123****``。
2. 路徑：``inbox/ review/ trash/ staging/ archive/`` 之後的每一個路徑段，只有「系統自己
   產生的名字」（staging 的 ``YYYY-MM-DD_HHMMSS_n[-k].ext``、archive 的
   ``YYYY-MM-DD_<rtype[-subtype]>_NN[-k].ext``、日期資料夾、``_unsorted``、已遮罩的識別碼、
   已雜湊名）以**精確樣式**（ASCII 類別，不用反斜線 w——它含中文）原樣保留，其餘（使用者取
   的檔名——LINE 下載檔名、含姓名的檔名）一律換成 ``h<8 碼雜湊><副檔名>``。引號內的路徑吃到
   引號為止（可含空白、支援 Windows repr 的雙反斜線）；**未加引號**的工作資料夾路徑沒有可
   判定的終點（檔名可含空白、前綴長得像系統名也不算證據）→ 一律從 token 起遮到行尾。程式自己
   印路徑請用 ``mask_path`` 並放在引號內。traceback 裡的路徑同樣適用。
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

# 工作資料夾 token：前後不得緊鄰 ASCII 英數（刻意不用 \w——Python 的 \w 含中文，
# 「收件夾inbox/…」這種前面黏中文的寫法也要抓到）。分隔符 [\\/]+ 允許 Windows 路徑在
# Python repr 裡的雙反斜線。
_WORKDIRS = ("inbox", "review", "trash", "staging", "archive")
_DIR_TOKEN = r"(?<![A-Za-z0-9_])(?:" + "|".join(_WORKDIRS) + r")(?![A-Za-z0-9_])"
_SEP = r"[\\/]+"
# 引號內的路徑：吃到對應引號為止，段內可含空白（例外訊息裡的路徑幾乎都是 repr 加引號）。
_QUOTED_PATH_RE = re.compile(
    r"(?P<q>['\"])(?P<pre>[^'\"]*?)(?P<dir>" + _DIR_TOKEN + r")(?P<rest>" + _SEP + r"[^'\"]*)(?P=q)"
)
_SEP_SPLIT_RE = re.compile(r"([\\/]+)")

# 系統自己產生、不含個資的路徑段：**精確樣式**（ASCII 類別，不用 \w），其餘一律視為
# 使用者取的名字。歸檔檔名的 rtype／subtype 詞彙與 extract.classify_report／archiver 一致；
# 詞彙擴充時這裡要同步，否則新類型檔名會被雜湊（安全方向的失效）。
_EXT = r"\.[A-Za-z0-9]{1,5}"
_SAFE_SEGMENT_RES = (
    re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_\d+(?:-\d+)?" + _EXT + r"$"),          # staging（batching.FILE_RE）
    re.compile(r"^\d{4}-\d{2}-\d{2}_(?:病灶照|檢驗(?:-(?:CBC|生化|尿液|過敏原))?|InBody|文件)_\d{2}(?:-\d+)?" + _EXT + r"$"),  # archive
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),                                          # 日期資料夾
    re.compile(r"^_unsorted$"),
    re.compile(r"^[A-Z][0-9]{5}\*{4}$"),                                          # 已遮罩證號
    re.compile(r"^P-[0-9]{3}\*{4}$"),                                             # 已遮罩暫時代號
    re.compile(r"^h[0-9a-f]{8}(?:" + _EXT + r")?$"),                              # 已雜湊（冪等）
)
_EXT_RE = re.compile(_EXT + r"$")
# 值已是 <redacted> 時原樣再吃掉一次，保持冪等（否則第二次淨化會變 <redacted><redacted>）。
_QUERY_RE = re.compile(r"([?&])([A-Za-z_][A-Za-z0-9_\-.]*)=(<redacted>|[^&\s\"'<>]*)")


def _mask_ids(text: str) -> str:
    return _PCODE_RE.sub(r"\1****", _ID_RE.sub(r"\1****", text))


def _hash_name(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    m = _EXT_RE.search(name)
    return f"h{digest}{m.group(0).lower() if m else ''}"


def _mask_segment(segment: str) -> tuple[str, bool]:
    """回 (淨化後, 是否為使用者取的名字而被雜湊)。"""
    seg = _mask_ids(segment)
    if any(r.match(seg) for r in _SAFE_SEGMENT_RES):
        return seg, False
    return _hash_name(segment), True


def mask_segment(segment: str) -> str:
    """單一路徑段／檔名：識別碼遮罩後，非系統產生的名字換成雜湊。"""
    return _mask_segment(segment)[0]


def _sanitize_rest(rest: str) -> tuple[str, bool]:
    out: list[str] = []
    arbitrary = False
    for piece in _SEP_SPLIT_RE.split(rest):
        if piece == "" or _SEP_SPLIT_RE.fullmatch(piece):
            out.append(piece)
            continue
        masked, hashed = _mask_segment(piece)
        arbitrary = arbitrary or hashed
        out.append(masked)
    return "".join(out), arbitrary


def _sub_quoted(m: re.Match) -> str:
    rest, _ = _sanitize_rest(m.group("rest"))
    return f"{m.group('q')}{m.group('pre')}{m.group('dir')}{rest}{m.group('q')}"


def mask_path(path: str | Path) -> str:
    """整個值就是一條路徑（邊界已知）：工作資料夾之後的段逐段淨化，其餘只遮識別碼。

    log 站點要印路徑時**必用本函式，並把結果放在引號內**（例：``"搬移 '%s' → '%s'"``），
    這樣 MaskingFormatter 的引號規則能確定邊界、保留後文；未加引號的路徑一律被遮到行尾。
    """
    text = _mask_ids(str(path))
    m = re.search(_DIR_TOKEN + r"(?=" + _SEP + r")", text)
    if m is None:
        return text
    rest, _ = _sanitize_rest(text[m.end():])
    return text[: m.end()] + rest


_PLACEHOLDER = "\x00{}\x00"


def _sanitize_line(line: str) -> str:
    """一行：識別碼 → 引號內路徑（邊界確定，逐段淨化）→ 未加引號路徑（一律遮到行尾）→ query 值。

    未加引號的路徑**沒有可判定的終點**：檔名可含空白，``inbox/2026-09-09 王小明.jpg`` 的前綴
    長得再像系統名也證明不了邊界（跨模型審查 R4）。所以不做任何前綴推斷，從工作資料夾 token
    起整段遮到行尾。程式自己要印路徑就用 mask_path＋引號。
    """
    line = _mask_ids(line)
    # 1) 引號內路徑先淨化並暫以佔位符取代，避免第 2 步把它當未加引號路徑整行砍掉。
    quoted: list[str] = []

    def _stash(m: re.Match) -> str:
        quoted.append(_sub_quoted(m))
        return _PLACEHOLDER.format(len(quoted) - 1)

    line = _QUOTED_PATH_RE.sub(_stash, line)
    # 2) 未加引號：第一個工作資料夾 token 起遮到行尾。
    m = re.search(_DIR_TOKEN + r"(?=" + _SEP + r")", line)
    if m is not None:
        sep = line[m.end()]
        line = line[: m.end()] + sep + "<redacted>"
    # 3) 還原引號內路徑。
    for i, q in enumerate(quoted):
        line = line.replace(_PLACEHOLDER.format(i), q)
    return _QUERY_RE.sub(r"\1\2=<redacted>", line)


def mask_text(text: str) -> str:
    """淨化任意輸出字串（多行逐行處理；traceback 亦適用）。"""
    if not text:
        return text
    return "\n".join(_sanitize_line(line) for line in text.split("\n"))


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
