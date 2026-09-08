"""批次還原（architecture.md §5 第 7 條（批次還原機制））——確定性 fail-closed 規則。

ClinicSnap 逐檔寫入、無批次 manifest。整合層以檔名特性重組批次，並用
「序號連續性」與「碰撞後綴」作為**確定性**的混批證據：寧可進佇列，絕不誤併。

檔名格式（ClinicSnap by_patient 輸出）::

    staging/{pid}/{YYYY-MM-DD}_{HHMMSS}_{idx}[-{coll}].{ext}

無 ID 批落在 ``staging/_unsorted/``（pid=None），格式相同。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 分組鍵：同一上傳請求共用同一時間戳（上游一請求取一次 now）。
# 群組：( pid, 日期, 時分秒 )；碰撞後綴 (-2/-3…) 由上游同名碰撞產生。
FILE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(\d{6})_(\d+)(?:-(\d+))?\.(jpe?g|png|webp|heic)$",
    re.I,
)

# suspect_reason 常數（繁中，直接給人看）
REASON_BAD_NAME = "檔名格式不明"
REASON_MIXED = "序號不連續或碰撞後綴＝疑混批"
REASON_STRAGGLER = "遲到檔"


@dataclass
class BatchGroup:
    key: str                     # "{pid|~}|{date}|{time}"（遲到檔偵測鍵）
    pid: str | None              # 資料夾名；_unsorted / 散落檔 → None
    date: str                    # YYYY-MM-DD（無法解析的檔名為 ""）
    time: str                    # HHMMSS（無法解析的檔名為 ""）
    files: list[Path] = field(default_factory=list)
    complete: bool = False       # 結構完整（idx 恰為 1..N 且無碰撞後綴）
    suspect_reason: str | None = None


def scan_staging(
    staging: Path,
    now: float | None = None,
    *,
    settle_seconds: int = 10,
    processed_keys: set[str] | None = None,
) -> list[BatchGroup]:
    """走訪 ``staging`` 一層子資料夾，重組批次並標注可疑原因。

    參數
    ----
    now:
        判定當下的 epoch 秒（預設 ``time.time()``）；用於靜置窗計算，測試可注入。
    settle_seconds:
        靜置窗（architecture §5 第 7 條（批次還原機制）預設 10）。組內最新 mtime
        距 ``now`` 未滿此秒數 →
        本輪跳過（不回傳），避免處理寫入中的半批。
    processed_keys:
        已處理批次鍵集合（呼叫端查 ``processed_batches`` 後傳入）。鍵已存在 →
        整組標 ``遲到檔``，交人工併回，**絕不**當新批自動歸檔。

    回傳的每一組都是 ``BatchGroup``；``suspect_reason`` 非 None 者由判準閘門一律進佇列。
    """
    if now is None:
        now = time.time()
    if processed_keys is None:
        processed_keys = set()

    staging = Path(staging)
    if not staging.exists():
        return []

    pending: dict[str, dict] = {}          # key -> {pid,date,time,files:[...]}
    garbage: list[tuple[str | None, Path, float]] = []

    def _add(f: Path, pid: str | None) -> None:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return
        m = FILE_RE.match(f.name)
        if not m:
            garbage.append((pid, f, mtime))
            return
        date, tm, idx, coll, _ext = (
            m.group(1), m.group(2), int(m.group(3)), m.group(4), m.group(5),
        )
        key = f"{pid or '~'}|{date}|{tm}"
        g = pending.setdefault(key, {"pid": pid, "date": date, "time": tm, "files": []})
        g["files"].append({"idx": idx, "coll": coll, "path": f, "mtime": mtime})

    for entry in sorted(staging.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue  # 略過 .DS_Store 等系統雜項（非臨床影像）
        if entry.is_dir():
            pid = None if entry.name == "_unsorted" else entry.name
            for f in sorted(entry.iterdir(), key=lambda p: p.name):
                if f.name.startswith(".") or not f.is_file():
                    continue
                _add(f, pid)
        elif entry.is_file():
            _add(entry, None)

    results: list[BatchGroup] = []

    for key, g in pending.items():
        files = g["files"]
        max_mtime = max(x["mtime"] for x in files)
        if now - max_mtime < settle_seconds:
            continue  # 靜置窗未過 → 本輪跳過（可能仍在寫入）
        ordered = sorted(files, key=lambda x: (x["idx"], x["coll"] or ""))
        paths = [x["path"] for x in ordered]

        has_coll = any(x["coll"] is not None for x in files)
        idxs = sorted(x["idx"] for x in files)
        n = len(files)
        complete = (not has_coll) and (idxs == list(range(1, n + 1)))

        # 精度順序：遲到檔（鍵已處理，確定性最強）> 序號/碰撞（混批證據）> 乾淨
        if key in processed_keys:
            reason = REASON_STRAGGLER
        elif not complete:
            reason = REASON_MIXED
        else:
            reason = None

        results.append(
            BatchGroup(
                key=key, pid=g["pid"], date=g["date"], time=g["time"],
                files=paths, complete=complete, suspect_reason=reason,
            )
        )

    for pid, f, mtime in garbage:
        if now - mtime < settle_seconds:
            continue
        results.append(
            BatchGroup(
                key=f"{pid or '~'}|badname|{f.name}",
                pid=pid, date="", time="", files=[f],
                complete=False, suspect_reason=REASON_BAD_NAME,
            )
        )

    results.sort(key=lambda b: b.key)
    return results
