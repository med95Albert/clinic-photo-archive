"""自動歸檔判準閘門（architecture.md §4）——單一實作點。

全系統唯一的「可否自動歸檔」決策。architecture §4 的四條判準同時滿足才 auto，
其餘一律進佇列（fail-closed 鐵律）：任何一格不滿足、狀態不明、解析失敗 → queue。

依賴（並行開發，故延遲載入 / 型別註記）：
  * ``clinic_archive.taiwan_id``：``verify_checksum`` / ``classify_manual_input``
    （T1；以 ``importlib`` 延遲載入，令本模組在 T1 未就緒時仍可 import，
    並讓單元測試能以 ``sys.modules`` 注入最小 stub）。
  * ``clinic_archive.extract.ReportFields``：僅型別註記，不在執行期 import。
"""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db

if TYPE_CHECKING:  # 僅供靜態檢查，不造成執行期硬依賴
    from clinic_archive.batching import BatchGroup
    from clinic_archive.extract import ReportFields


@dataclass
class Verdict:
    auto_file: bool
    patient_key: str | None
    reason: str


@dataclass
class ImageEvidence:
    """批內單一張圖的獨立證據（逐圖，不跨圖匯總）。

    * ``path``   ：該圖的檔案路徑（歸檔／佇列時據以搬移）。
    * ``ids``    ：該圖 OCR 出的證號「格式層」候選（checksum 由判準套用）。
    * ``is_card``：該圖是否偵測為健保卡（``extract.detect_card``）。
    * ``dob``    ：該圖讀到的生日（ISO）；判準只採信 ``is_card`` 圖上的生日。

    逐圖保存證據是為了要求「證號與卡面生日同源」——避免 A 圖的證號和 B 圖的
    生日被跨圖湊成假吻合而歸錯人（本次修法的核心）。
    """

    path: Path
    ids: list[str] = field(default_factory=list)
    is_card: bool = False
    dob: str | None = None


def _tid():
    """延遲取得 taiwan_id 模組（測試可經 sys.modules 注入 stub）。"""
    return importlib.import_module("clinic_archive.taiwan_id")


def _get_patient(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """直接 SQL（不依賴 T2 的 db.py DAO；不動用 conn.row_factory）。"""
    row = conn.execute(
        "SELECT patient_key, name, dob, chart_no FROM patients WHERE patient_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return {"patient_key": row[0], "name": row[1], "dob": row[2], "chart_no": row[3]}


def _resolve_pid(conn: sqlite3.Connection, pid: str):
    """解析手機端帶入的病患代碼 → (resolved_key | None, queue_reason | None)。

    queue_reason 非 None 代表 pid 本身無法解析（步驟 2 直接進佇列）。
    resolved_key 為該 pid 對應的正規 patient_key（national_id 情形不保證已建檔，
    是否已建檔由後續步驟判定）。
    """
    tid = _tid()
    kind, value = tid.classify_manual_input(pid)
    if kind == "national_id":
        # classify 已保證格式符且 checksum 過；key 即證號本身。
        return value, None
    if kind == "pcode":
        if _get_patient(conn, value) is not None:
            return value, None
        return None, "暫時代號查無此人"
    if kind == "chart_no":
        # 病歷號無唯一約束（HIS 別名可能重號）：查全部命中，恰一才可據以歸檔。
        # 0 筆（查無）或 >1 筆（重號）一律 fail-closed，避免 LIMIT 1 盲配。
        rows = db.find_patients_by_chart(conn, value)
        if len(rows) == 1:
            # 位置存取（patient_key 為 patients 表首欄）：與 _get_patient 一致，
            # 不強制 conn.row_factory，維持本模組對 row_factory 的獨立性。
            return rows[0][0], None
        return None, "病歷號對應不唯一或不存在"
    # kind == 'invalid'
    return None, "手機端身份代碼無法解析"


def _distinct_candidates(ids: list[str]) -> set[str]:
    """OCR 證號「格式層」候選集合（去重，**不先過濾 checksum**）。

    恰一原則（architecture §4 第 1 條）在**格式層**計數：任何 ``[A-Z][0-9]{9}``
    候選都算一個證號，未通過檢查碼的也算。之前的作法是先濾掉 checksum 不過的
    候選再數，結果「一張文件上有兩位病人，其中一位的證號被 OCR 誤讀一碼」會被
    當成恰一而自動歸給另一位（跨模型審查 2026-09-09 實證重現）。代價是報告上
    形如「字母＋9 位數」的檢體／報告編號會讓該份報告進佇列——這是 fail-closed
    的刻意選擇，v1 若實測佇列量過高再加可設定的雜訊白名單。
    """
    return {i for i in ids if i}


def decide_photo_batch(
    conn: sqlite3.Connection,
    group: "BatchGroup",
    images: list[ImageEvidence],
) -> Verdict:
    """照片批判準（architecture §4 + §5；依序）——逐圖證據、要求同源。

    ``images`` 是批內每一張圖的獨立證據。判準不再把全批證號與生日「匯總」後
    比對——那會讓 A 圖的證號與 B 圖的卡面生日跨圖湊成一筆假吻合而歸錯人
    （本次修法的核心攻擊情境）。故：
      * 「恰一原則」仍以全批唯一有效證號為前提；
      * 「卡面生日驗證」要求證號與生日**出自同一張** is_card 的圖；
      * 「手機端人工背書」分支不變（唯一證號 == 解析後 pid 對應病人）；
      * 證號只出現在非卡圖而卡圖另有生日等任何不同源組合 → fail-closed 佇列。
    """
    # 1) 批次還原已標可疑（序號/碰撞/遲到/檔名）→ 直接佇列。
    if group.suspect_reason:
        return Verdict(False, None, group.suspect_reason)

    # 2) 解析手機端帶入代碼（若有）。解析失敗/查無/重號 → 佇列。
    resolved_key: str | None = None
    if group.pid is not None:
        resolved_key, reason = _resolve_pid(conn, group.pid)
        if reason is not None:
            return Verdict(False, None, reason)

    # 3) 候選證號 = 全批各圖 OCR 格式層候選的聯集（去重、不先濾 checksum）。恰一原則。
    all_ids = [i for img in images for i in img.ids]
    candidates = _distinct_candidates(all_ids)
    if len(candidates) > 1:
        return Verdict(False, None, "批內多個證號")

    # 4) 恰一證號 → 才驗 checksum（§4 第 2 條），再驗已建檔與第二驗證。
    if len(candidates) == 1:
        cid = next(iter(candidates))
        if not _tid().verify_checksum(cid):
            return Verdict(False, None, "證號未通過檢查碼")
        patient = _get_patient(conn, cid)
        if patient is None:
            # 首見證號一律人工建檔（§4 第 3 層防呆）。
            return Verdict(False, None, "首見證號")

        # N0 首張錨定（architecture §5）：錨點卡必須是批次的第一張。
        # images 依 reports._ocr_batch 按檔名序號順序建立，images[0] 即 N0 位置；
        # 卡片證據（本證號或生日）出現在其他位置＝未遵循 N0 協定或多卡混批 → 人工。
        first = images[0] if images else None
        first_is_anchor = bool(first is not None and first.is_card and cid in first.ids)
        any_later_card = any(img.is_card for img in images[1:])

        if any_later_card:
            # 第二張（含以後）出現任何卡片影像：可能是兩位病人的卡混批、或未遵循
            # N0 首張協定。即使首張卡驗證全過也不得放行——後卡可能屬於另一位病人
            # （審查 R4 P0：A 卡首張全吻合＋B 卡在後，整批誤歸 A）。
            return Verdict(False, None, "卡片非首張或多卡影像需人工")

        if first_is_anchor and first.dob:
            # N0 卡自帶生日（同圖同源）→ 獨立第二驗證。
            if patient["dob"] is None:
                return Verdict(False, None, "建檔資料缺生日，無法交叉核對")
            if first.dob == patient["dob"]:
                return Verdict(True, cid, "證號已建檔＋卡面生日吻合")
            return Verdict(False, None, "生日不符")

        if first_is_anchor:
            # N0 卡在首張但生日不可讀 → 手機端人工帶入代碼＝與 OCR 無關的背書。
            if resolved_key == cid:
                return Verdict(True, cid, "證號已建檔＋手機端人工背書一致")
            return Verdict(False, None, "卡面生日不可讀且手機端代碼未背書")

        if any(img.is_card for img in images):
            # 卡片只在首張但本證號不出自它（證號來自文件照等）→ 跨圖不同源。
            return Verdict(False, None, "證據不同源需人工")
        return Verdict(False, None, "證號僅見於非卡片影像需人工")

    # 5) 零證號（無卡批）。無複掃證號可比對 → fail closed。
    if resolved_key is not None or group.pid is not None:
        # 手機端帶了身份線索，但伺服器複掃不到卡 → 需人工一鍵確認。
        return Verdict(False, None, "無卡批需人工一鍵確認")
    return Verdict(False, None, "無任何身份線索")


def decide_report(conn: sqlite3.Connection, fields: "ReportFields") -> Verdict:
    """檢驗報告判準（architecture §4）——報告必有生日，生日必查。"""
    candidates = _distinct_candidates(list(fields.ids))
    if len(candidates) == 0:
        return Verdict(False, None, "報告無有效證號")
    if len(candidates) > 1:
        return Verdict(False, None, "報告內多個證號")

    cid = next(iter(candidates))
    if not _tid().verify_checksum(cid):
        return Verdict(False, None, "報告證號未通過檢查碼")
    patient = _get_patient(conn, cid)
    if patient is None:
        return Verdict(False, None, "首見證號")

    if not fields.dob:
        return Verdict(False, None, "報告缺生日")
    if patient["dob"] is None:
        return Verdict(False, None, "建檔資料缺生日，無法交叉核對")
    if fields.dob == patient["dob"]:
        return Verdict(True, cid, "報告證號已建檔＋生日吻合")
    return Verdict(False, None, "生日不符")
