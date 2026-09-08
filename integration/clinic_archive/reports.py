"""管線處理層（SPEC §10 的 reports.py 部分）。

把散在各模組的能力串成兩條「掃描 → 判準 → 歸檔 / 佇列」的處理流程：

* ``process_staging``：ClinicSnap 手機端逐檔寫入的 ``staging/`` 照片批。經 batching
  還原批次、逐圖 OCR 建 ``ImageEvidence``（證號、是否健保卡、卡面生日），交 predicate
  以「逐圖同源」判準裁決；auto 則逐檔一律歸為 ``病灶照``，偵測到卡的圖不論批內張數
  都掛 ``card_suspect`` 佇列項供管理者裁決（純卡→移除／N0 同框→保留），否則整組進
  ``review/`` 並記佇列。
* ``process_inbox``：桌面掃描／匯入落在 ``inbox/`` 的檢驗報告。PDF 與非白名單副檔名
  直接進佇列（v0 不做 PDF OCR）；影像則 OCR→抽欄位→判準，auto 歸檔否則佇列。

fail-closed 鐵律貫穿全檔：任何 OCR／解析例外、判準不滿足、狀態不明 → 一律進佇列，
絕不臆測、絕不覆蓋、絕不刪除。批次處理完畢一律 ``mark_batch`` 以支援遲到檔偵測。

OCR 引擎經 ``ocr.ocr_image_text`` 呼叫（以模組屬性存取，讓測試能 monkeypatch 免真引擎）。
DB 寫入走 T2 的 ``db`` DAO 與 T4 的 ``archiver``；判準走 T4 的 ``predicate`` 單一實作點。
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

from . import archiver, db, extract, ocr, predicate, taiwan_id
from .batching import REASON_STRAGGLER, scan_staging

logger = logging.getLogger(__name__)

# 佇列原因常數（繁中，直接給人看）。
REASON_PDF = "PDF 需人工"
REASON_UNSUPPORTED = "格式不支援"
REASON_OCR_FAILED = "OCR 失敗需人工"
REASON_CARD_SUSPECT = "健保卡影像建議人工刪除"


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------
def _processed_keys(conn) -> set[str]:
    """查 processed_batches 取全部批次鍵，供 scan_staging 判遲到檔（整合注意 2）。"""
    rows = conn.execute("SELECT batch_key FROM processed_batches").fetchall()
    return {r[0] for r in rows}


def _fields_payload(fields) -> dict:
    """把 ReportFields 攤平成 JSON-safe dict（keywords set → 排序 list）供 UI 顯示。

    連 ``classify_report`` 的 (rtype, subtype) 一起寫入：自動歸檔路徑會呼叫
    classify_report 分類，但進佇列的路徑以前只存原始欄位，等人工 resolve 時
    ``webapp._filing_params`` 讀不到 ``extracted.rtype`` 就一律退回「文件」——
    同一份 CBC 報告，自動歸檔是「檢驗/CBC」、經佇列卻變「文件」，分類在人工
    確認的當下反而流失了。分類結果在這裡就算好，兩條路徑才會一致。
    """
    rtype, subtype = extract.classify_report(fields.keywords)
    return {
        "ids": list(fields.ids),
        "names": list(fields.names),
        "dob": fields.dob,
        "chart_no": fields.chart_no,
        "report_date": fields.report_date,
        "keywords": sorted(fields.keywords),
        "rtype": rtype,
        "subtype": subtype,
    }


# ---------------------------------------------------------------------------
# inbox：檢驗報告
# ---------------------------------------------------------------------------
def _queue_inbox_file(conn, cfg, src: Path, reason: str, extracted: dict) -> None:
    """單一 inbox 檔進佇列：搬入 review/ 後記 report 佇列項（payload 含 extracted）。"""
    dest = archiver.move_to_review(cfg, src)
    payload = {"files": [str(dest)], "extracted": extracted, "src": "inbox"}
    db.add_queue_item(conn, "report", reason, payload)


def process_inbox(conn, cfg, now: float | None = None) -> dict:
    """掃描 ``inbox/`` 一輪，回傳 ``{"auto", "queued", "skipped"}`` 計數。

    靜置窗：檔案 mtime 距 ``now`` 未滿 ``cfg.settle_seconds`` → 本輪跳過（可能仍在寫入）。
    ``now`` 預設為現在時刻，測試可注入以精確控制靜置判定（免 sleep）。
    """
    counts = {"auto": 0, "queued": 0, "skipped": 0}
    inbox = Path(cfg.data_root) / "inbox"
    if not inbox.exists():
        return counts
    if now is None:
        now = time.time()

    allowed = {e.lower() for e in cfg.allowed_exts}
    settle = cfg.settle_seconds

    for f in sorted(inbox.iterdir(), key=lambda p: p.name):
        if f.name.startswith(".") or not f.is_file():
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - mtime < settle:
            counts["skipped"] += 1
            continue

        ext = f.suffix.lower()
        # PDF 與非白名單副檔名：不做 OCR，直接佇列（v0 誠實限制）。
        if ext == ".pdf":
            _queue_inbox_file(conn, cfg, f, REASON_PDF, {})
            counts["queued"] += 1
            continue
        if ext not in allowed:
            _queue_inbox_file(conn, cfg, f, REASON_UNSUPPORTED, {})
            counts["queued"] += 1
            continue

        # 影像：OCR → 抽欄位 → 判準。任何例外一律 fail-closed 進佇列。
        try:
            text = ocr.ocr_image_text(f, cfg)
            fields = extract.extract_report_fields(text)
            verdict = predicate.decide_report(conn, fields)
        except Exception:
            logger.exception("inbox OCR／解析失敗，改進佇列：%s", f)
            _queue_inbox_file(conn, cfg, f, REASON_OCR_FAILED, {})
            counts["queued"] += 1
            continue

        if verdict.auto_file and verdict.patient_key:
            rtype, subtype = extract.classify_report(fields.keywords)
            taken = fields.report_date or date.fromtimestamp(mtime).isoformat()
            archiver.file_record(
                conn, cfg, f, verdict.patient_key, taken, rtype, subtype, "inbox", None
            )
            counts["auto"] += 1
        else:
            _queue_inbox_file(conn, cfg, f, verdict.reason, _fields_payload(fields))
            counts["queued"] += 1

    return counts


# ---------------------------------------------------------------------------
# staging：照片批
# ---------------------------------------------------------------------------
def _ocr_batch(cfg, group) -> list["predicate.ImageEvidence"]:
    """對一組批次逐張 OCR，回傳每張圖的 ``ImageEvidence``（逐圖證據，不跨圖匯總）。

    證號用 taiwan_id.extract_ids 抽（checksum 由 predicate 套用）；健保卡以 detect_card
    偵測；卡面生日**只對 is_card 的圖**抽取（非卡圖的雜訊日期不採信，避免被拿去跨圖
    湊吻合）。可能拋 OCR 例外，由呼叫端 fail-closed 收斂。
    """
    images: list[predicate.ImageEvidence] = []
    for f in group.files:
        text = ocr.ocr_image_text(f, cfg)
        is_card = extract.detect_card(text)
        dob = extract.extract_report_fields(text).dob if is_card else None
        images.append(
            predicate.ImageEvidence(
                path=f, ids=taiwan_id.extract_ids(text), is_card=is_card, dob=dob
            )
        )
    return images


def _auto_file_batch(conn, cfg, group, patient_key: str, card_files: set) -> None:
    """auto 分支：逐檔一律歸為 ``病灶照``（偵測到卡的圖不再自動標『識別影像』）。

    偵測到卡的圖**不論批內張數**（含單張純卡批）都另建 ``card_suspect`` 佇列項
    （payload 含歸檔後路徑與 record id），供管理者裁決「純卡→移除」或「N0 同框→保留」。
    v0 不自動刪卡（與 docs 差異，README 註明）。
    """
    for f in group.files:
        dest = archiver.file_record(
            conn, cfg, f, patient_key, group.date, "病灶照", None, "phone", group.key
        )
        if f in card_files:
            row = conn.execute("SELECT id FROM records WHERE path=?", (str(dest),)).fetchone()
            rid = row[0] if row is not None else None
            db.add_queue_item(
                conn,
                "card_suspect",
                REASON_CARD_SUSPECT,
                {
                    "files": [str(dest)],
                    "record_ids": [rid],
                    "patient_key": patient_key,
                    "batch_key": group.key,
                },
            )
    db.mark_batch(conn, group.key, "auto_filed")


def _queue_batch(conn, cfg, group, reason: str, all_ids: list[str], card_dob: str | None) -> None:
    """queue 分支：整組搬入 review/ 並記佇列項（遲到檔記 straggler，其餘 photo_batch）。"""
    review_paths = [str(archiver.move_to_review(cfg, f)) for f in group.files]
    kind = "straggler" if group.suspect_reason == REASON_STRAGGLER else "photo_batch"
    payload = {
        "files": review_paths,
        "extracted": {"ids": sorted(set(all_ids)), "card_dob": card_dob},
        "batch_key": group.key,
        "pid": group.pid,
        "date": group.date,
    }
    db.add_queue_item(conn, kind, reason, payload)
    db.mark_batch(conn, group.key, "queued")


def _process_one_batch(conn, cfg, group, counts: dict) -> None:
    images: list[predicate.ImageEvidence] = []

    # 可疑批（序號/碰撞/遲到/檔名）直接交判準佇列，免 OCR（也避開對非影像垃圾檔硬解）。
    if not group.suspect_reason:
        try:
            images = _ocr_batch(cfg, group)
        except Exception:
            logger.exception("staging OCR 失敗，整組改進佇列：%s", group.key)
            _queue_batch(conn, cfg, group, REASON_OCR_FAILED, [], None)
            counts["queued"] += 1
            return

    verdict = predicate.decide_photo_batch(conn, group, images)
    if verdict.auto_file and verdict.patient_key:
        card_files = {img.path for img in images if img.is_card}
        _auto_file_batch(conn, cfg, group, verdict.patient_key, card_files)
        counts["auto"] += 1
    else:
        # 佇列 payload 供 UI 顯示：攤平全批證號候選＋任一張卡的卡面生日（僅資訊性）。
        all_ids = [i for img in images for i in img.ids]
        card_dob = next((img.dob for img in images if img.is_card and img.dob), None)
        _queue_batch(conn, cfg, group, verdict.reason, all_ids, card_dob)
        counts["queued"] += 1


def process_staging(conn, cfg, now: float | None = None) -> dict:
    """掃描 ``staging/`` 一輪，回傳 ``{"auto", "queued"}`` 計數。

    先查 processed_batches 取已處理鍵集合傳入 ``scan_staging``（判遲到檔）；每組已過靜置窗
    者逐一處理。``now`` 透傳給 ``scan_staging`` 供靜置窗計算，測試可注入（免 sleep）。
    """
    counts = {"auto": 0, "queued": 0}
    staging = Path(cfg.data_root) / "staging"
    if not staging.exists():
        return counts

    groups = scan_staging(
        staging,
        now,
        settle_seconds=cfg.settle_seconds,
        processed_keys=_processed_keys(conn),
    )
    for group in groups:
        _process_one_batch(conn, cfg, group, counts)
    return counts
