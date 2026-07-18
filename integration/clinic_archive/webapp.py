"""Web UI 層（SPEC §11，工單 T6）。

FastAPI ＋ jinja2 的地端管理介面：登入、佇列審核、病人時間軸、帳號與稽核。
本層只負責「呈現與人工動作」，所有搬檔／判準／DAO 皆呼叫既有模組，不自行重造。

整合要點
--------
* ``create_app(cfg) -> FastAPI``：main.py 以此建立 app。cfg 以 duck-typing 讀
  ``cfg.data_root``／``cfg.db_path``／``cfg.session_hours``（不硬性 import config.py）。
* **每個 request 開一條新的 sqlite 連線、用完關閉**（``get_conn`` 依賴注入）。
  sqlite 連線不可跨執行緒共用，FastAPI 以 threadpool 跑 sync route，故最安全的做法
  就是 request-scope 連線；sqlite 開連線很便宜，這點成本可接受。
* 安全紅線：
  - ``/file/{record_id}`` 只送 records 表內登記的 path，且必須 resolve 到 data_root
    之下（防路徑穿越）；records 外的 record_id 或落在 data_root 外的 path 一律拒絕。
  - 佇列縮圖走 ``/queue/{id}/file/{n}``：n 只是 payload["files"] 的索引（路徑非使用者
    可控），另 resolve 後確認落在 data_root 之下才送。
  - session cookie ``httponly`` ＋ ``samesite=lax``；所有 POST 動作驗 session＋角色；
    登入失敗與所有人工動作皆寫 audit。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import archiver, auth, db, extract, taiwan_id

logger = logging.getLogger(__name__)

COOKIE_NAME = "clinic_session"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# 佇列 kind → 繁中顯示名。
KIND_LABELS: dict[str, str] = {
    "photo_batch": "照片批次",
    "report": "檢驗報告",
    "straggler": "遲到散檔",
    "card_suspect": "疑似證件照",
}

# payload["extracted"] 常見欄位 → 繁中標籤（顯示用；未列出的 key 原樣顯示）。
EXTRACTED_LABELS: dict[str, str] = {
    "ids": "證號候選",
    "names": "姓名",
    "dob": "生日",
    "chart_no": "病歷號",
    "report_date": "報告日",
    "keywords": "關鍵字",
    "rtype": "類型",
    "subtype": "子類",
}


# ---------------------------------------------------------------------------
# 認證：request-scope 連線、目前使用者、角色閘門
# ---------------------------------------------------------------------------


class _AuthRequired(Exception):
    """未登入時由依賴丟出；由 app 的 exception handler 轉為導向 /login。"""


def get_conn(request: Request):
    """每個 request 開一條新的 sqlite 連線，回應送出後關閉（見模組 docstring）。"""
    cfg = request.app.state.cfg
    conn = db.connect(cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)) -> dict[str, str]:
    """回傳 ``{'username', 'role', 'csrf'}``；cookie 無效／過期／查無使用者 → 丟 _AuthRequired。

    一併帶出 session 列的 csrf token（Fix-A 已在 sessions 表加欄並於建 session 時配發）：
    GET 頁把它塞進表單 hidden 欄，POST 路由以 ``secrets.compare_digest`` 比對，
    達成 double-submit CSRF 防護。改用 ``db.get_session`` 取代 ``auth.check_session``
    只為多拿一個 csrf 欄，過期刪除語意完全相同。
    """
    token = request.cookies.get(COOKIE_NAME)
    if token:
        sess = db.get_session(conn, token)
        if sess is not None:
            row = db.get_user(conn, sess["username"])
            if row is not None:
                return {
                    "username": sess["username"],
                    "role": row["role"],
                    "csrf": sess["csrf"] or "",
                }
    raise _AuthRequired()


def require_manager(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    """管理者專屬路由的閘門：非 manager 一律 403（已登入但權限不足）。"""
    if user["role"] != "manager":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理員權限")
    return user


# ---------------------------------------------------------------------------
# 路徑安全輔助
# ---------------------------------------------------------------------------


def _within(child: Path, parent: Path) -> bool:
    """child resolve 後是否位於 parent 之下（含 parent 本身）。防 ``..`` 穿越。"""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _data_root(request: Request) -> Path:
    return Path(request.app.state.cfg.data_root)


def _move_to_trash(cfg: Any, src: Path) -> Path:
    """把實體檔搬到 ``{data_root}/trash/``（保留原名、衝突加後綴、絕不覆蓋）。

    同磁碟 ``os.replace``；跨磁碟 fallback = copy2＋unlink。不寫 DB（呼叫端負責）。
    """
    return archiver.claimed_move(src, Path(cfg.data_root) / "trash")


# ---------------------------------------------------------------------------
# CSRF 與佇列原子認領輔助
# ---------------------------------------------------------------------------


def _verify_csrf(user: dict[str, str], csrf: str, conn) -> None:
    """比對表單 csrf 與 session csrf（constant-time）；不符 → audit('csrf_reject')＋403。

    登入表單例外（尚無 session），其餘每個 POST route 進入時都先過這關。
    ``secrets.compare_digest`` 對兩個空字串會回 True，故先確認 expected／csrf 皆非空
    再比對，避免「session 無 csrf＋表單也沒帶」被誤判為通過。
    """
    expected = user.get("csrf") or ""
    ok = bool(expected) and bool(csrf) and secrets.compare_digest(str(csrf), str(expected))
    if not ok:
        db.add_audit(conn, actor=user["username"], action="csrf_reject", detail="CSRF 驗證失敗")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 驗證失敗")


def _persist_partial(conn, item_id: int, created_key: str | None, filed_paths: list[str]) -> None:
    """write-ahead 進度落盤（審查 R4 P0）：病人一建立／每歸一檔就立刻寫進 payload.partial。

    硬崩潰（斷電、kill -9）不會走 except handler——事後開機 reconcile 只會把項目
    退回 open；payload.partial 是唯一能告訴下一位操作者「已建了誰、歸了幾檔」的
    憑據，因此必須在副作用發生的當下就落盤，而不是等例外才寫。
    """
    row = conn.execute("SELECT payload FROM queue_items WHERE id=?", (item_id,)).fetchone()
    pl = json.loads(row["payload"]) if row else {}
    prior = pl.get("partial") or {}
    # 合併語義（審查 R5）：created_key=None（existing 續歸）不得抹掉先前記錄的
    # 病人代碼——否則其後崩潰會讓「封鎖再新建」閘門失效；created_key=""（哨兵）
    # ＝明確清除意圖（P 碼撞號時用，避免幽靈意圖指向無辜的既有病人）。
    if created_key == "":
        key = None
    else:
        key = created_key or prior.get("patient_key")
    pl["partial"] = {"patient_key": key, "filed": list(filed_paths)}
    conn.execute("UPDATE queue_items SET payload=? WHERE id=?",
                 (json.dumps(pl, ensure_ascii=False), item_id))
    conn.commit()


def _revert_claim_if_processing(conn, item_id: int) -> None:
    """把仍卡在 'processing' 的認領退回 open（動作已完成者不受影響，冪等）。

    審查 R2 P1：認領後任何一步失敗若不退回，項目會以 resolved/processing 永遠
    消失在佇列之外。條件式 WHERE 讓成功路徑與已退回路徑呼叫皆為 no-op。
    """
    conn.execute(
        "UPDATE queue_items SET state='open', resolved_by=NULL, resolved_at=NULL, "
        "resolution=NULL WHERE id=? AND state='resolved' AND resolution='processing'",
        (item_id,),
    )
    conn.commit()


def _finish_resolution(conn, item_id: int, resolution: str) -> None:
    """認領成功、動作完成後，把 queue_items.resolution 由 'processing' 覆寫為實際結果。"""
    conn.execute("UPDATE queue_items SET resolution = ? WHERE id = ?", (resolution, item_id))
    conn.commit()


# ---------------------------------------------------------------------------
# 歸檔參數推導（佇列 payload → file_record 參數）
# ---------------------------------------------------------------------------


def _filing_params(kind: str, payload: dict[str, Any]) -> tuple[str, str, str | None, str]:
    """依佇列 kind／payload 推導 (taken_date, rtype, subtype, src)。

    - photo_batch／card_suspect：病灶照、phone。
    - report／straggler：型別取 extracted.rtype/subtype（無則『文件』）、inbox。
    taken_date 依序取：payload.taken_date → extracted.report_date → batch_key 內日期 → 今日。
    """
    extracted = payload.get("extracted") or {}
    if kind in ("report", "straggler"):
        rtype = extracted.get("rtype") or "文件"
        subtype = extracted.get("subtype")
        src = "inbox"
    else:  # photo_batch / card_suspect
        rtype, subtype, src = "病灶照", None, "phone"

    taken_date = _resolve_taken_date(payload, extracted)
    return taken_date, rtype, subtype, src


def _resolve_taken_date(payload: dict[str, Any], extracted: dict[str, Any]) -> str:
    _iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    for cand in (payload.get("taken_date"), extracted.get("report_date")):
        if isinstance(cand, str) and _iso.fullmatch(cand):
            return cand
    batch_key = payload.get("batch_key") or ""
    parts = batch_key.split("|")
    if len(parts) >= 2 and _iso.fullmatch(parts[1]):
        return parts[1]
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

router = APIRouter()


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    ctx = dict(context)
    ctx.setdefault("kind_labels", KIND_LABELS)
    ctx.setdefault("extracted_labels", EXTRACTED_LABELS)
    return request.app.state.templates.TemplateResponse(
        request, name, ctx, status_code=status_code
    )


def _already_processed(request: Request, user: dict[str, str]):
    """原子認領失敗（項目已被別的請求處理）→ 200 友善頁，絕不做任何歸檔。"""
    return _render(
        request, "already_processed.html",
        {"user": user, "message": "此項已被處理"}, status_code=200,
    )


# --- 登入／登出 -------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    return _render(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    conn=Depends(get_conn),
):
    user = db.get_user(conn, username)
    if user is None or not auth.verify_pw(password, user["pwhash"]):
        # 登入失敗：寫 audit（actor 記所嘗試的帳號）＋ log，回登入頁附錯誤。
        db.add_audit(conn, actor=username or "?", action="login", detail="登入失敗")
        logger.warning("登入失敗：username=%s", username)
        return _render(
            request, "login.html", {"error": "帳號或密碼錯誤"}, status_code=401
        )

    session_hours = request.app.state.cfg.session_hours
    token = auth.new_session(conn, username, session_hours)
    db.add_audit(conn, actor=username, action="login", detail="登入成功")
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    # cookie 壽命對齊伺服器端 session TTL；伺服器端 check_session 為過期的最終依據。
    resp.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=int(session_hours * 3600),
    )
    return resp


@router.post("/logout")
def logout(request: Request, csrf: str = Form(""), conn=Depends(get_conn)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        sess = db.get_session(conn, token)
        if sess is not None:
            expected = sess["csrf"] or ""
            if not (expected and secrets.compare_digest(str(csrf), str(expected))):
                db.add_audit(
                    conn, actor=sess["username"], action="csrf_reject", detail="logout CSRF 驗證失敗"
                )
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 驗證失敗")
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            db.add_audit(conn, actor=sess["username"], action="login", detail="登出")
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- 佇列總覽 ---------------------------------------------------------------


@router.get("/")
def overview(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    items = db.open_queue_items(conn)
    grouped: dict[str, list] = {}
    for it in items:
        grouped.setdefault(it["kind"], []).append(it)
    return _render(
        request, "overview.html", {"user": user, "grouped": grouped, "total": len(items)}
    )


# --- 佇列詳情 ＋ resolve -----------------------------------------------------


def _load_open_item(conn, item_id: int):
    """只取仍 open 的佇列項（GET 詳情／縮圖用）。已 resolved 或不存在 → 404。

    加上 ``AND state='open'`` 是 Fix-B P0 的一環：resolved 後的項目不應再被詳情頁
    當作可處理對象顯示（避免對已歸檔項重放動作）。POST resolve 不走這裡，另以
    原子認領 UPDATE 判定，並對「已被處理」回友善 200 頁。
    """
    row = conn.execute(
        "SELECT * FROM queue_items WHERE id = ? AND state = 'open'", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="佇列項目不存在或已處理")
    return row


def _detail_context(request: Request, conn, item, user, *, q: str = "", error: str | None = None):
    payload = json.loads(item["payload"])
    files = payload.get("files", []) or []
    extracted = payload.get("extracted") or {}
    results = db.list_patients(conn, search=q) if q else None
    return {
        "user": user,
        "item": item,
        "payload": payload,
        "files": list(enumerate(files)),
        "extracted": extracted,
        "batch_key": payload.get("batch_key"),
        "q": q,
        "results": results,
        "error": error,
        "is_card_suspect": item["kind"] == "card_suspect",
    }


@router.get("/queue/{item_id}")
def queue_detail(
    item_id: int,
    request: Request,
    q: str = "",
    user=Depends(require_manager),
    conn=Depends(get_conn),
):
    item = _load_open_item(conn, item_id)
    ctx = _detail_context(request, conn, item, user, q=q)
    return _render(request, "queue_detail.html", ctx)


@router.get("/queue/{item_id}/file/{index}")
def queue_file(
    item_id: int,
    index: int,
    request: Request,
    user=Depends(require_manager),
    conn=Depends(get_conn),
):
    """佇列縮圖：只依 payload["files"] 的索引取檔，並確認落在 data_root 之下。"""
    item = _load_open_item(conn, item_id)
    files = (json.loads(item["payload"]).get("files") or [])
    if not (0 <= index < len(files)):
        raise HTTPException(status_code=404, detail="索引超出範圍")
    path = Path(files[index])
    if not _within(path, _data_root(request)):
        raise HTTPException(status_code=403, detail="路徑越界")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="檔案不存在")
    return FileResponse(str(path))


@router.post("/queue/{item_id}/resolve")
def queue_resolve(
    item_id: int,
    request: Request,
    action: str = Form(...),
    patient_key: str = Form(""),
    patient_id: str = Form(""),
    name: str = Form(""),
    dob: str = Form(""),
    csrf: str = Form(""),
    user=Depends(require_manager),
    conn=Depends(get_conn),
):
    """佇列裁決：CSRF → 輸入驗證 → 原子認領 → 執行動作 → 覆寫 resolution。

    防重放鐵律（P0）：真正改動狀態前先做一次
    ``UPDATE ... SET state='resolved', resolution='processing' WHERE id=? AND state='open'``；
    ``rowcount!=1`` 代表已被別的請求／執行緒認領 → 回「此項已被處理」友善頁，
    **不做任何歸檔**。認領成功者才建病人／歸檔／刪卡，最後把 resolution 覆寫為實際結果。

    輸入驗證刻意在認領之前：驗不過（證號檢查碼、查無既有病人、生日格式、卡片動作
    用錯佇列）一律重填表單（400）且**不認領**，佇列維持 open 供修正——認領只在
    確定要動手時才發生，避免因表單錯誤把項目卡在 resolved。
    """
    _verify_csrf(user, csrf, conn)
    cfg = request.app.state.cfg
    actor = user["username"]

    # 直接載入（不限 state）：真的不存在才 404；已處理走友善 200 頁。
    item = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="佇列項目不存在")
    if item["state"] != "open":
        return _already_processed(request, user)

    payload = json.loads(item["payload"])
    files = payload.get("files", []) or []
    is_card = item["kind"] == "card_suspect"
    partial_key = (payload.get("partial") or {}).get("patient_key")

    # ---- 1) 輸入驗證（失敗 → 重填表單、不認領、佇列維持 open）----
    # card_suspect 的 payload 指向「已歸檔」的檔案：一般歸檔動作會把 N0 搬進
    # 另一位病人資料夾、留下指向空路徑的原 record（審查 R2 P0）→ 一律拒絕。
    if is_card and action not in ("card_keep", "delete_card"):
        ctx = _detail_context(request, conn, item, user, error="疑似證件照僅能選『保留』或『移除』")
        return _render(request, "queue_detail.html", ctx, status_code=400)
    dob_norm: str | None = None
    target_key: str | None = None

    if partial_key and db.get_patient(conn, partial_key) is None:
        # 幽靈意圖（審查 R5）：write-ahead 先寫了意圖、病人卻從未建成（崩潰在
        # insert 之前）→ 該意圖作廢，否則操作者會被鎖死在一個不存在的代號上。
        logger.warning("queue#%s partial 意圖 %s 無對應病人，視為作廢", item_id, partial_key)
        partial_key = None

    partial_filed = (payload.get("partial") or {}).get("filed") or []
    if partial_key and action in ("new_id", "new_pcode"):
        # 已建過病人 → 擋再新建（避免第二位幽靈病人）。
        ctx = _detail_context(
            request, conn, item, user,
            error=f"此項先前已建病人 {partial_key}，請用「既有病人」選擇該代號歸檔，系統已封鎖再新建",
        )
        return _render(request, "queue_detail.html", ctx, status_code=400)
    if partial_key and partial_filed and not (
        action == "existing" and patient_key.strip() == partial_key
    ):
        # 已有檔案實際歸到 partial_key → 硬綁定：只能續歸同一位（審查 R5 P0）。
        # filed 為空時不硬綁（意圖可能來自撞號等待清除的瞬間，強綁反而危險），
        # 但上面的「擋再新建」仍然生效。
        ctx = _detail_context(
            request, conn, item, user,
            error=f"此項先前已部分歸檔至 {partial_key}，重試僅能用「既有病人」選擇同一代號續歸，其他選項已封鎖",
        )
        return _render(request, "queue_detail.html", ctx, status_code=400)

    if action in ("card_keep", "delete_card"):
        if not is_card:
            ctx = _detail_context(request, conn, item, user, error="此動作僅適用疑似證件照")
            return _render(request, "queue_detail.html", ctx, status_code=400)
    elif action == "existing":
        key = patient_key.strip()
        if not key or db.get_patient(conn, key) is None:
            ctx = _detail_context(request, conn, item, user, error="查無此病人，請重新搜尋")
            return _render(request, "queue_detail.html", ctx, status_code=400)
        target_key = key
    elif action == "new_id":
        kind, value = taiwan_id.classify_manual_input(patient_id)
        if kind not in ("national_id", "pcode"):
            ctx = _detail_context(
                request, conn, item, user,
                error="請輸入通過檢查碼的身分證號，或改用『一鍵配 P 碼』",
            )
            return _render(request, "queue_detail.html", ctx, status_code=400)
        if dob.strip():
            dob_norm = extract.normalize_date(dob)
            if dob_norm is None:
                ctx = _detail_context(
                    request, conn, item, user,
                    error="生日格式無法辨識，請用 YYYY-MM-DD、114/05/04、民國90年5月4日 等格式",
                )
                return _render(request, "queue_detail.html", ctx, status_code=400)
        target_key = value
    elif action == "new_pcode":
        if dob.strip():
            dob_norm = extract.normalize_date(dob)
            if dob_norm is None:
                ctx = _detail_context(
                    request, conn, item, user,
                    error="生日格式無法辨識，請用 YYYY-MM-DD、114/05/04、民國90年5月4日 等格式",
                )
                return _render(request, "queue_detail.html", ctx, status_code=400)
        # target_key 於認領後才配號（避免驗證階段配號、卻因競爭落敗而留下幽靈病人）。
    else:
        ctx = _detail_context(request, conn, item, user, error="未知的動作")
        return _render(request, "queue_detail.html", ctx, status_code=400)

    # ---- 2) 原子認領：只有一個請求能把 open→resolved ----
    claimed = conn.execute(
        "UPDATE queue_items SET state='resolved', resolved_by=?, "
        "resolved_at=datetime('now','localtime'), resolution='processing' "
        "WHERE id=? AND state='open'",
        (actor, item_id),
    )
    conn.commit()
    if claimed.rowcount != 1:
        return _already_processed(request, user)

    created_key: str | None = None   # 本次嘗試中新建的病人（部分完成追蹤用）
    filed_paths: list[str] = []       # 本次嘗試中已歸檔的檔案
    try:
        # ---- 3) 認領成功，執行實際動作 ----

        # 3a) card_suspect：保留（卡＋病灶同框 N0）／純卡片照移除。
        if action == "card_keep":
            db.add_audit(
                conn, actor=actor, action="card_keep",
                detail=f"queue#{item_id} 保留卡＋病灶同框（N0），檔案與紀錄不動",
            )
            _finish_resolution(conn, item_id, "card_keep：保留（卡＋病灶同框 N0）")
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

        if action == "delete_card":
            moved = 0
            for f in files:
                p = Path(f)
                if _within(p, _data_root(request)) and p.is_file():
                    _move_to_trash(cfg, p)
                    moved += 1
                # 移除指向此實體檔的 records 列（檔已入 trash，可回收，非永久刪除）。
                conn.execute("DELETE FROM records WHERE path = ?", (f,))
            conn.commit()
            db.add_audit(conn, actor=actor, action="delete_card", detail=f"queue#{item_id} 移除卡片照 {moved} 檔")
            _finish_resolution(conn, item_id, f"deleted_card：移除卡片照 {moved} 檔")
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

        # 3b) 建／選病人。existing 絕不寫 patients；建新一律走 insert_patient_strict。
        reused_existing = False
        if action == "new_id":
            try:
                # write-ahead（審查 R5）：意圖先落盤，崩潰在 insert 前後都有跡可循
                #（insert 前崩潰＝幽靈意圖，重試時自動作廢）。
                _persist_partial(conn, item_id, target_key, filed_paths)
                db.insert_patient_strict(conn, target_key, name or None, dob_norm, None, actor)
                db.add_audit(
                    conn, actor=actor, action="create_patient", patient_key=target_key,
                    detail=f"name={name or '—'} dob={dob_norm or '—'} chart=—",
                )
                created_key = target_key
            except sqlite3.IntegrityError:
                # 證號已建檔 → 視同選既有：不改其資料、直接歸檔。
                reused_existing = True
        elif action == "new_pcode":
            # 認領式配號迴圈：next_pcode 讀最大值＋1，撞號（併發同秒）→ IntegrityError → 重取。
            target_key = None
            for _ in range(20):
                candidate = taiwan_id.next_pcode(conn)
                try:
                    _persist_partial(conn, item_id, candidate, filed_paths)
                    db.insert_patient_strict(conn, candidate, name or None, dob_norm, None, actor)
                    target_key = candidate
                    break
                except sqlite3.IntegrityError:
                    # 撞號＝該代號屬於既有病人（非本次所建）→ 立刻清除意圖，
                    # 免得此刻崩潰讓閘門把操作者綁到無辜病人（審查 R5 角案）。
                    _persist_partial(conn, item_id, "", filed_paths)
                    continue
            if target_key is None:
                # 迴圈耗盡（極端併發）：回滾認領讓項目重回 open 可再處理，回 500。
                conn.execute(
                    "UPDATE queue_items SET state='open', resolved_by=NULL, "
                    "resolved_at=NULL, resolution=NULL WHERE id=?",
                    (item_id,),
                )
                conn.commit()
                raise HTTPException(status_code=500, detail="P 碼配號連續碰撞，請稍後重試")
            db.add_audit(
                conn, actor=actor, action="create_patient", patient_key=target_key,
                detail=f"name={name or '—'} dob={dob_norm or '—'} chart=—",
            )
            created_key = target_key
        # action == "existing"：target_key 已定，無 patients 寫入。
        if action == "existing" and partial_key:
            created_key = partial_key  # 續歸：讓後續 write-ahead 保留原記錄代號

        # ---- 4) 歸檔前驗檔案存在：缺檔記明、其餘照歸（不得靜默跳過）----
        present, missing = [], []
        for f in files:
            (present if Path(f).is_file() else missing).append(f)
        if missing:
            names = "、".join(Path(m).name for m in missing)
            db.add_audit(
                conn, actor=actor, action="resolve", patient_key=target_key,
                detail=f"queue#{item_id} 缺檔 {len(missing)}（未歸檔）：{names}",
            )
            logger.warning("resolve 缺檔 %d：%s", len(missing), names)

        taken_date, rtype, subtype, src = _filing_params(item["kind"], payload)
        batch_key = payload.get("batch_key")
        filed = 0
        for f in present:
            dest = archiver.file_record(
                conn, cfg, Path(f), target_key, taken_date, rtype, subtype,
                src, batch_key, actor=actor,
            )
            # archiver.file_record 一律寫入 status='auto'；人工確認的紀錄補記為 'confirmed'。
            conn.execute("UPDATE records SET status = 'confirmed' WHERE path = ?", (str(dest),))
            conn.commit()
            filed_paths.append(str(dest))
            _persist_partial(conn, item_id, created_key, filed_paths)
            filed += 1

        notes = []
        if reused_existing:
            notes.append("證號已建檔，沿用既有資料")
        if missing:
            notes.append(f"缺檔 {len(missing)} 未歸")
        resolution = f"confirmed:{target_key}" + (("（" + "；".join(notes) + "）") if notes else "")
        _finish_resolution(conn, item_id, resolution)

        db.add_audit(
            conn, actor=actor, action="resolve", patient_key=target_key,
            detail=f"queue#{item_id} 歸檔 {filed} 檔 → {target_key}"
            + (f"（缺 {len(missing)}）" if missing else ""),
        )
        return RedirectResponse(f"/p/{target_key}", status_code=status.HTTP_303_SEE_OTHER)

    except HTTPException:
        _revert_claim_if_processing(conn, item_id)
        raise
    except Exception as exc:  # noqa: BLE001
        # 部分完成追蹤（審查 R3 P0）：已建病人／已歸檔案是「已提交」的事實，退回
        # open 後重試若再走 new_pcode 會建出第二個病人、把一批裂成兩夾。把部分
        # 進度寫回 payload 與 reason，介面引導操作者重試時改選「既有病人」。
        if created_key or filed_paths:
            try:
                _persist_partial(conn, item_id, created_key, filed_paths)
                _note = "｜⚠️ 部分完成：" + "、".join(filter(None, [
                    f"已建病人 {created_key}" if created_key else None,
                    f"已歸 {len(filed_paths)} 檔" if filed_paths else None,
                ])) + "——重試請選「既有病人」，勿再新建"
                conn.execute(
                    "UPDATE queue_items SET reason=reason || ? WHERE id=?",
                    (_note, item_id),
                )
                conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("queue#%s 記錄部分進度失敗", item_id)
        _revert_claim_if_processing(conn, item_id)
        db.add_audit(conn, actor=actor, action="resolve_error",
                     detail=f"queue#{item_id} 處理失敗已退回佇列：{exc}")
        logger.exception("queue#%s 裁決失敗，已退回佇列", item_id)
        raise HTTPException(status_code=500, detail="處理失敗，項目已退回佇列") from exc


# --- 病人搜尋 ---------------------------------------------------------------


@router.get("/patients")
def patients(
    request: Request,
    q: str = "",
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    results = db.list_patients(conn, search=q) if q else db.list_patients(conn)
    return _render(request, "patients.html", {"user": user, "q": q, "results": results})


# --- 病人時間軸 -------------------------------------------------------------


@router.get("/p/{key}")
def timeline(
    key: str,
    request: Request,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    patient = db.get_patient(conn, key)
    if patient is None:
        raise HTTPException(status_code=404, detail="病人不存在")
    records = db.records_for_patient(conn, key)
    # 依 taken_date 分組（DAO 已 ORDER BY taken_date, id）。
    days: dict[str, list] = {}
    for r in records:
        days.setdefault(r["taken_date"], []).append(r)
    db.add_audit(conn, actor=user["username"], action="view_timeline", patient_key=key)
    return _render(
        request, "timeline.html",
        {"user": user, "patient": patient, "days": days, "count": len(records)},
    )


# --- 受控送檔（records 內路徑，限 data_root 之下）---------------------------


@router.get("/file/{record_id}")
def serve_file(
    record_id: int,
    request: Request,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    row = conn.execute(
        "SELECT path, patient_key FROM records WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="紀錄不存在")
    path = Path(row["path"])
    if not _within(path, _data_root(request)):
        # records 內竟登記了 data_root 外的 path → 視為異常，拒絕（防穿越）。
        raise HTTPException(status_code=403, detail="路徑越界")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="檔案不存在")
    db.add_audit(
        conn, actor=user["username"], action="view_file",
        patient_key=row["patient_key"], detail=path.name,
    )
    return FileResponse(str(path))


# --- 帳號管理 ---------------------------------------------------------------


@router.get("/users")
def users_list(request: Request, user=Depends(require_manager), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT username, role, created_at FROM users ORDER BY username"
    ).fetchall()
    return _render(request, "users.html", {"user": user, "users": rows, "error": None})


@router.post("/users")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    csrf: str = Form(""),
    user=Depends(require_manager),
    conn=Depends(get_conn),
):
    _verify_csrf(user, csrf, conn)
    username = username.strip()
    error = None
    if not username or not password:
        error = "帳號與密碼皆為必填"
    elif role not in ("viewer", "manager"):
        error = "角色只能是 viewer 或 manager"
    elif db.get_user(conn, username) is not None:
        error = "帳號已存在"
    if error:
        rows = conn.execute(
            "SELECT username, role, created_at FROM users ORDER BY username"
        ).fetchall()
        return _render(
            request, "users.html", {"user": user, "users": rows, "error": error},
            status_code=400,
        )

    db.create_user(conn, username, auth.hash_pw(password), role)
    db.add_audit(
        conn, actor=user["username"], action="create_user",
        detail=f"建立帳號 {username}（{role}）",
    )
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


# --- 稽核 -------------------------------------------------------------------


@router.get("/audit")
def audit_log(request: Request, user=Depends(require_manager), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT ts, actor, action, patient_key, detail FROM audit ORDER BY id DESC LIMIT 500"
    ).fetchall()
    return _render(request, "audit.html", {"user": user, "rows": rows})


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_app(cfg: Any) -> FastAPI:
    """建立並回傳 FastAPI app（main.py 以 ``create_app(cfg)`` 呼叫）。

    cfg 存入 ``app.state.cfg`` 供依賴／路由讀取；DB 於此做一次 idempotent init_db，
    確保即使呼叫端未先 init 也能運作（main.py 亦會 init，重複無害）。
    """
    app = FastAPI(title="診所照片歸檔")
    app.state.cfg = cfg
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # 防禦性 init：request-scope 連線之外，先確保 schema 就緒。
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()

    @app.exception_handler(_AuthRequired)
    async def _auth_redirect(request: Request, exc: _AuthRequired) -> Response:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    app.include_router(router)
    return app
