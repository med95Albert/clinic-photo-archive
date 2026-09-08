"""T6 測試：webapp.py（FastAPI TestClient）＋ Fix-B 佇列/CSRF 缺陷回歸。

以 tmp_path 建立整套環境（config／db／假影像檔），涵蓋 SPEC §11 與 Fix-B：
* 未登入一律 302 導向 /login。
* viewer 不得 POST resolve／建帳號（403）。
* manager 全流程：建病人 → resolve photo_batch → records 落庫（status='confirmed'）
  → 時間軸看得到 → /file 可取檔。
* /file 對「records 外的 id」與「落在 data_root 外的 path」皆拒絕。
* card_suspect 兩動作：保留（card_keep）／純卡片照移除（delete_card）。
* 登入失敗寫入 audit。

Fix-B 專項（point 6）：
* double-resolve 第二次回「已處理」且不產生新病人。
* 並發 resolve 只成功一個（兩 thread 各自連線）。
* P-code 併發不撞號（threading barrier）。
* resolve 既有病人後其 dob/name 不變。
* csrf 缺失／錯誤 403。
* card_suspect 兩動作各自行為。
* dob 亂格式被表單擋下（不入庫）。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clinic_archive import archiver, auth, config, db, ocr, reports, webapp

# 已知通過內政部檢查碼的身分證號（A=10，加權和 130 ≡ 0 mod 10）。
VALID_ID = "A123456789"
PHOTO_BATCH_KEY = f"{VALID_ID}|2026-07-18|120000"


# ---------------------------------------------------------------------------
# 環境建置
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path):
    data_root = tmp_path / "clinic_data"
    cfg = config.AppConfig(data_root=str(data_root))
    # AppConfig() 不會展開 {data_root} 佔位符，測試裡手動展開成實際路徑。
    cfg = dataclasses.replace(cfg, db_path=str(data_root / "clinic.db"))
    config.ensure_dirs(cfg)
    return cfg


@dataclasses.dataclass
class _Env:
    cfg: object
    app: object
    data_root: object


@pytest.fixture()
def env(tmp_path):
    cfg = _make_cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.create_user(conn, "boss", auth.hash_pw("boss-pw"), "manager")
    db.create_user(conn, "eye", auth.hash_pw("eye-pw"), "viewer")
    conn.close()

    app = webapp.create_app(cfg)
    return _Env(cfg=cfg, app=app, data_root=tmp_path / "clinic_data")


def _conn(env):
    return db.connect(env.cfg.db_path)


def _login(app, username, password):
    client = TestClient(app)
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _csrf(env, client):
    """從 sessions 表取出此 client 目前 session 的 csrf token（供 POST 表單帶入）。"""
    token = client.cookies.get(webapp.COOKIE_NAME)
    conn = _conn(env)
    row = conn.execute("SELECT csrf FROM sessions WHERE token = ?", (token,)).fetchone()
    conn.close()
    assert row is not None, "找不到 session；client 尚未登入？"
    return row["csrf"]


def _post(env, client, url, data=None, **kwargs):
    """已登入的 POST：自動帶入正確 csrf（除非 data 已顯式指定）。"""
    payload = dict(data or {})
    payload.setdefault("csrf", _csrf(env, client))
    kwargs.setdefault("follow_redirects", False)
    return client.post(url, data=payload, **kwargs)


def _fake_image(path, content=b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpeg"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _add_queue(env, kind, reason, files, extracted=None, batch_key=PHOTO_BATCH_KEY):
    conn = _conn(env)
    payload = {"files": [str(f) for f in files], "extracted": extracted or {}, "batch_key": batch_key}
    item_id = db.add_queue_item(conn, kind, reason, payload)
    conn.close()
    return item_id


# ---------------------------------------------------------------------------
# 1) 未登入導向
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/patients", "/p/whoever", "/queue/1", "/users", "/audit"])
def test_unauthenticated_redirects_to_login(env, path):
    client = TestClient(env.app)
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_login_page_is_public(env):
    client = TestClient(env.app)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "登入" in resp.text


# ---------------------------------------------------------------------------
# 2) 登入成功／失敗與 audit
# ---------------------------------------------------------------------------


def test_login_success_sets_cookie_and_reaches_overview(env):
    client = _login(env.app, "boss", "boss-pw")
    assert webapp.COOKIE_NAME in client.cookies

    resp = client.get("/")
    assert resp.status_code == 200
    assert "待確認佇列" in resp.text

    conn = _conn(env)
    row = conn.execute(
        "SELECT * FROM audit WHERE action='login' AND detail='登入成功' AND actor='boss'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_login_failure_is_audited(env):
    client = TestClient(env.app)
    resp = client.post(
        "/login",
        data={"username": "boss", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "帳號或密碼錯誤" in resp.text
    assert webapp.COOKIE_NAME not in client.cookies

    conn = _conn(env)
    row = conn.execute(
        "SELECT * FROM audit WHERE action='login' AND detail='登入失敗' AND actor='boss'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_session_cookie_is_httponly_and_lax(env):
    client = TestClient(env.app)
    resp = client.post(
        "/login", data={"username": "boss", "password": "boss-pw"}, follow_redirects=False
    )
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_login_issues_session_csrf(env):
    # 登入成功後 session 列應帶一枚非空 csrf（GET 頁與 POST 表單防護的來源）。
    client = _login(env.app, "boss", "boss-pw")
    csrf = _csrf(env, client)
    assert isinstance(csrf, str) and len(csrf) >= 16
    # 佇列詳情頁的表單應內嵌 csrf hidden 欄。
    review_file = _fake_image(env.data_root / "review" / "c.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])
    resp = client.get(f"/queue/{item_id}")
    assert resp.status_code == 200
    assert 'name="csrf"' in resp.text
    assert csrf in resp.text


# ---------------------------------------------------------------------------
# 3) viewer 權限限制
# ---------------------------------------------------------------------------


def test_viewer_cannot_post_resolve(env):
    review_file = _fake_image(env.data_root / "review" / "v.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "eye", "eye-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "new_pcode"})
    # 角色閘門先於 csrf 驗證：viewer 一律 403。
    assert resp.status_code == 403


def test_viewer_cannot_access_manager_pages(env):
    client = _login(env.app, "eye", "eye-pw")
    assert client.get("/users").status_code == 403
    assert client.get("/audit").status_code == 403

    item_id = _add_queue(env, "photo_batch", "首見證號", [])
    assert client.get(f"/queue/{item_id}").status_code == 403

    resp = _post(env, client, "/users", {"username": "x", "password": "y", "role": "viewer"})
    assert resp.status_code == 403


def test_viewer_can_view_overview_and_patients(env):
    client = _login(env.app, "eye", "eye-pw")
    assert client.get("/").status_code == 200
    assert client.get("/patients").status_code == 200


# ---------------------------------------------------------------------------
# 4) manager 全流程（建新病人 → resolve → 落庫 → 時間軸 → /file）
# ---------------------------------------------------------------------------


def test_queued_report_keeps_classification_through_manual_resolve(env, monkeypatch):
    """報告進佇列 → 人工 resolve → rtype 仍是「檢驗」、subtype 保留。

    迴歸：`reports._fields_payload` 以前不寫 rtype/subtype，`webapp._filing_params`
    讀不到就退回「文件」。結果同一份 CBC 報告，自動歸檔是「檢驗/CBC」，經佇列人工
    確認卻變成「文件」——分類在人工確認的當下反而流失。這條 e2e 走完整條路徑：
    真的 process_inbox 產生 payload、真的 webapp resolve 歸檔，兩端都不造假。
    """
    report_text = (
        "檢驗報告\n姓名 林小華\n"
        f"{VALID_ID}\n出生 2015-05-05\n報告日期 2026-07-11\nCBC 白血球 血紅素"
    )
    inbox_file = env.data_root / "inbox" / "report.jpg"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_text(report_text, encoding="utf-8")

    # 免真 OCR 引擎：直接把檔案文字當作 OCR 逐行結果。
    monkeypatch.setattr(
        ocr, "ocr_image_text",
        lambda path_or_bytes, cfg: Path(path_or_bytes).read_text(encoding="utf-8"),
    )
    cfg = dataclasses.replace(env.cfg, settle_seconds=0)

    conn = _conn(env)
    counts = reports.process_inbox(conn, cfg)
    conn.close()
    # VALID_ID 尚未建檔 → 首見證號 → 進佇列（正是分類會流失的那條路徑）。
    assert counts["queued"] == 1 and counts["auto"] == 0

    conn = _conn(env)
    item = conn.execute("SELECT id, payload FROM queue_items").fetchone()
    conn.close()
    # payload 必須已帶分類，人工 resolve 才有東西可用。
    payload = json.loads(item["payload"])
    assert payload["extracted"]["rtype"] == "檢驗"
    assert payload["extracted"]["subtype"] == "CBC"

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(
        env, client, f"/queue/{item['id']}/resolve",
        {"action": "new_id", "patient_id": VALID_ID, "name": "林小華", "dob": "2015-05-05"},
    )
    assert resp.status_code == 303, resp.text

    conn = _conn(env)
    records = db.records_for_patient(conn, VALID_ID)
    conn.close()
    assert len(records) == 1
    rec = records[0]
    assert rec["rtype"] == "檢驗", "經佇列人工歸檔的檢驗報告不該退化成「文件」"
    assert rec["subtype"] == "CBC"
    assert rec["src"] == "inbox"
    # 檔名也應反映分類（archiver 以 rtype-subtype 命名）。
    assert "檢驗-CBC" in Path(rec["path"]).name


def test_manager_resolve_photo_batch_full_flow(env):
    review_file = _fake_image(env.data_root / "review" / "photo.jpg")
    item_id = _add_queue(
        env, "photo_batch", "首見證號", [review_file],
        extracted={"ids": [VALID_ID], "keywords": []},
    )

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(
        env, client, f"/queue/{item_id}/resolve",
        {"action": "new_id", "patient_id": VALID_ID, "name": "王小明", "dob": "2015-01-01"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/p/{VALID_ID}"

    conn = _conn(env)
    patient = db.get_patient(conn, VALID_ID)
    assert patient is not None and patient["name"] == "王小明"

    records = db.records_for_patient(conn, VALID_ID)
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "confirmed"  # 人工確認 → confirmed（非預設 auto）
    assert rec["rtype"] == "病灶照"
    assert rec["taken_date"] == "2026-07-18"

    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    assert item["state"] == "resolved"

    resolve_audit = conn.execute(
        "SELECT * FROM audit WHERE action='resolve' AND patient_key=?", (VALID_ID,)
    ).fetchone()
    # 建新病人一律留 create_patient 稽核（含 name/dob）。
    create_audit = conn.execute(
        "SELECT * FROM audit WHERE action='create_patient' AND patient_key=?", (VALID_ID,)
    ).fetchone()
    conn.close()
    assert resolve_audit is not None
    assert create_audit is not None and "王小明" in create_audit["detail"]

    # 實體檔已由 review 搬入 archive。
    from pathlib import Path
    assert not review_file.exists()
    assert Path(rec["path"]).is_file()

    # 時間軸看得到。
    tl = client.get(f"/p/{VALID_ID}")
    assert tl.status_code == 200
    assert "2026-07-18" in tl.text

    # /file 取得檔案內容。
    served = client.get(f"/file/{rec['id']}")
    assert served.status_code == 200
    assert served.content == Path(rec["path"]).read_bytes()


def test_manager_resolve_with_new_pcode(env):
    review_file = _fake_image(env.data_root / "review" / "nocard.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "new_pcode", "name": "無名氏"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/P-0000001"

    conn = _conn(env)
    records = db.records_for_patient(conn, "P-0000001")
    conn.close()
    assert len(records) == 1
    assert records[0]["status"] == "confirmed"


def test_manager_resolve_existing_patient(env):
    conn = _conn(env)
    db.upsert_patient(conn, "P-0000009", name="舊病人")
    conn.close()

    review_file = _fake_image(env.data_root / "review" / "exist.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "existing", "patient_key": "P-0000009"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/P-0000009"

    conn = _conn(env)
    assert len(db.records_for_patient(conn, "P-0000009")) == 1
    conn.close()


def test_resolve_rejects_bad_id_checksum(env):
    review_file = _fake_image(env.data_root / "review" / "bad.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(
        env, client, f"/queue/{item_id}/resolve",
        {"action": "new_id", "patient_id": "A123456780"},  # checksum 不過
    )
    assert resp.status_code == 400
    assert "檢查碼" in resp.text
    # 檔案未被搬動，佇列仍開啟（驗證失敗不認領）。
    assert review_file.exists()
    conn = _conn(env)
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert item["state"] == "open"


# ---------------------------------------------------------------------------
# 5) /file 安全紅線
# ---------------------------------------------------------------------------


def test_file_route_rejects_tampered_record_id(env):
    client = _login(env.app, "eye", "eye-pw")
    resp = client.get("/file/999999")
    assert resp.status_code == 404


def test_file_route_rejects_path_outside_data_root(env, tmp_path):
    # 在 records 內植入一筆 path 落在 data_root 之外的紀錄，/file 必須拒絕。
    outside = tmp_path / "outside_root.jpg"  # tmp_path 是 data_root 的上一層
    _fake_image(outside)

    conn = _conn(env)
    db.upsert_patient(conn, "P-0000001", name="測試")
    rec_id = db.insert_record(
        conn, patient_key="P-0000001", taken_date="2026-07-18", rtype="病灶照",
        subtype=None, src="phone", path=str(outside), sha256="x", status="auto",
    )
    conn.close()

    client = _login(env.app, "eye", "eye-pw")
    resp = client.get(f"/file/{rec_id}")
    assert resp.status_code == 403


def test_queue_thumbnail_confined_to_payload_index(env):
    review_file = _fake_image(env.data_root / "review" / "thumb.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    ok = client.get(f"/queue/{item_id}/file/0")
    assert ok.status_code == 200
    assert ok.content == review_file.read_bytes()

    # 索引超出範圍 → 404（無法用來讀任意檔）。
    assert client.get(f"/queue/{item_id}/file/9").status_code == 404


def test_queue_thumbnail_is_audited(env):
    """佇列縮圖＝查閱病人影像，必須寫 audit。

    文件承諾「經系統介面的查閱都有紀錄」。/file/{record_id} 一直有寫，
    /queue/{id}/file/{n} 卻沒有——等於歸檔前的影像可以被看光而稽核表一片空白。
    """
    review_file = _fake_image(env.data_root / "review" / "audited.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    assert client.get(f"/queue/{item_id}/file/0").status_code == 200

    conn = _conn(env)
    row = conn.execute(
        "SELECT * FROM audit WHERE action='view_file' AND actor='boss'"
    ).fetchone()
    conn.close()
    assert row is not None, "佇列縮圖查閱未留下 audit"
    assert f"queue#{item_id}" in row["detail"]
    assert "第 0 檔" in row["detail"]


def test_queue_thumbnail_rejected_requests_are_not_audited(env):
    """只有真正送出檔案才記查閱；404／403 不該灌水稽核表。"""
    review_file = _fake_image(env.data_root / "review" / "notaudited.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    assert client.get(f"/queue/{item_id}/file/9").status_code == 404

    conn = _conn(env)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE action='view_file'"
    ).fetchone()["n"]
    conn.close()
    assert n == 0


# ---------------------------------------------------------------------------
# 6) card_suspect：保留（card_keep）／移除（delete_card）
# ---------------------------------------------------------------------------


def _seed_card_item(env, filename, rtype="病灶照"):
    """建立一個含實體歸檔卡片檔與 record 的 card_suspect 佇列項，回傳 (item_id, dest, rec_id)。"""
    conn = _conn(env)
    db.upsert_patient(conn, VALID_ID, name="王小明", dob="2015-01-01")
    card_src = _fake_image(env.data_root / "review" / filename)
    dest = archiver.file_record(
        conn, env.cfg, card_src, VALID_ID, "2026-07-18", rtype, None,
        "phone", PHOTO_BATCH_KEY, actor="system",
    )
    rec_id = conn.execute("SELECT id FROM records WHERE path=?", (str(dest),)).fetchone()["id"]
    item_id = db.add_queue_item(
        conn, "card_suspect", "疑似證件照",
        {"files": [str(dest)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY},
    )
    conn.close()
    return item_id, dest, rec_id


def test_card_suspect_delete_flow(env):
    item_id, dest, rec_id = _seed_card_item(env, "card.jpg")
    assert dest.is_file()

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "delete_card"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # 實體檔離開 archive、進入 trash。
    assert not dest.exists()
    trash_files = list((env.data_root / "trash").iterdir())
    assert len(trash_files) == 1
    # 內容原封不動搬入 trash（非永久刪除，可回收）。
    assert trash_files[0].read_bytes() == b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpeg"

    conn = _conn(env)
    # 紀錄移除、佇列 resolved、audit('delete_card') 有記錄。
    assert conn.execute("SELECT 1 FROM records WHERE id=?", (rec_id,)).fetchone() is None
    item = conn.execute("SELECT state, resolution FROM queue_items WHERE id=?", (item_id,)).fetchone()
    assert item["state"] == "resolved"
    assert "deleted_card" in item["resolution"]
    del_audit = conn.execute("SELECT * FROM audit WHERE action='delete_card'").fetchone()
    conn.close()
    assert del_audit is not None


def test_card_suspect_keep_flow(env):
    # 保留：檔案與 record 一律不動，只結案並 audit('card_keep')。
    item_id, dest, rec_id = _seed_card_item(env, "keep.jpg")
    assert dest.is_file()

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "card_keep"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"

    # 實體檔與 record 原封不動。
    assert dest.is_file()
    conn = _conn(env)
    assert conn.execute("SELECT 1 FROM records WHERE id=?", (rec_id,)).fetchone() is not None
    item = conn.execute("SELECT state, resolution FROM queue_items WHERE id=?", (item_id,)).fetchone()
    assert item["state"] == "resolved"
    assert "card_keep" in item["resolution"]
    keep_audit = conn.execute("SELECT 1 FROM audit WHERE action='card_keep'").fetchone()
    conn.close()
    assert keep_audit is not None

    # trash 應保持空（沒有任何刪除）。
    trash = env.data_root / "trash"
    trash_files = list(trash.iterdir()) if trash.exists() else []
    assert trash_files == []


def test_delete_card_rejected_on_non_card_queue(env):
    review_file = _fake_image(env.data_root / "review" / "np.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "delete_card"})
    assert resp.status_code == 400
    assert review_file.exists()
    conn = _conn(env)
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert item["state"] == "open"  # 動作用錯佇列，不認領


def test_card_suspect_detail_shows_both_actions(env):
    from pathlib import Path

    item_id, dest, _ = _seed_card_item(env, "cardview.jpg")

    client = _login(env.app, "boss", "boss-pw")
    resp = client.get(f"/queue/{item_id}")
    assert resp.status_code == 200
    # 兩個動作皆呈現（以 hidden action 值判定，對按鈕文案穩健）。
    assert 'value="card_keep"' in resp.text
    assert 'value="delete_card"' in resp.text
    assert "保留" in resp.text and "移除" in resp.text
    assert isinstance(Path(dest), Path)  # dest 為實體檔路徑


# ---------------------------------------------------------------------------
# 7) Fix-B P0：原子認領 / 防重放 / 併發
# ---------------------------------------------------------------------------


def test_double_resolve_second_says_already_processed_no_new_patient(env):
    # double-submit：第一次成功歸檔並建 P 碼；第二次回「已處理」，且不再產生幽靈病人。
    review_file = _fake_image(env.data_root / "review" / "double.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    csrf = _csrf(env, client)

    r1 = client.post(
        f"/queue/{item_id}/resolve",
        data={"action": "new_pcode", "csrf": csrf}, follow_redirects=False,
    )
    assert r1.status_code == 303
    assert r1.headers["location"] == "/p/P-0000001"

    r2 = client.post(
        f"/queue/{item_id}/resolve",
        data={"action": "new_pcode", "csrf": csrf}, follow_redirects=False,
    )
    assert r2.status_code == 200
    assert "已被處理" in r2.text

    conn = _conn(env)
    pcodes = conn.execute(
        "SELECT patient_key FROM patients WHERE patient_key LIKE 'P-%'"
    ).fetchall()
    # 沒有 P-0000002 這種幽靈病人。
    assert [r["patient_key"] for r in pcodes] == ["P-0000001"]
    assert len(db.records_for_patient(conn, "P-0000001")) == 1
    conn.close()


def test_concurrent_resolve_same_item_only_one_succeeds(env):
    # 兩管理者（各自連線）同秒 resolve 同一項 → 只成功一個，另一個回「已處理」。
    review_file = _fake_image(env.data_root / "review" / "dup.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name):
        client = _login(env.app, "boss", "boss-pw")
        csrf = _csrf(env, client)
        barrier.wait()
        results[name] = client.post(
            f"/queue/{item_id}/resolve",
            data={"action": "new_pcode", "csrf": csrf}, follow_redirects=False,
        )

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 303]  # 恰一成功、一個「已處理」

    conn = _conn(env)
    pcodes = conn.execute("SELECT patient_key FROM patients WHERE patient_key LIKE 'P-%'").fetchall()
    assert len(pcodes) == 1  # 只建了一位病人
    assert len(db.records_for_patient(conn, "P-0000001")) == 1  # 只歸檔一筆
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert item["state"] == "resolved"


def test_concurrent_new_pcode_distinct_codes(env):
    # 兩個不同佇列項、兩管理者同秒 new_pcode → 必須配到不同 P 碼（認領式配號迴圈防撞）。
    f1 = _fake_image(env.data_root / "review" / "p1.jpg")
    f2 = _fake_image(env.data_root / "review" / "p2.jpg")
    item1 = _add_queue(env, "photo_batch", "無任何身份線索", [f1])
    item2 = _add_queue(env, "photo_batch", "無任何身份線索", [f2])

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name, item_id):
        client = _login(env.app, "boss", "boss-pw")
        csrf = _csrf(env, client)
        barrier.wait()
        results[name] = client.post(
            f"/queue/{item_id}/resolve",
            data={"action": "new_pcode", "csrf": csrf}, follow_redirects=False,
        )

    threads = [
        threading.Thread(target=worker, args=("a", item1)),
        threading.Thread(target=worker, args=("b", item2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["a"].status_code == 303
    assert results["b"].status_code == 303
    locs = {results["a"].headers["location"], results["b"].headers["location"]}
    assert locs == {"/p/P-0000001", "/p/P-0000002"}  # 兩個不同 P 碼、皆成功

    conn = _conn(env)
    pcodes = conn.execute(
        "SELECT patient_key FROM patients WHERE patient_key LIKE 'P-%' ORDER BY patient_key"
    ).fetchall()
    assert [r["patient_key"] for r in pcodes] == ["P-0000001", "P-0000002"]
    for pk in ("P-0000001", "P-0000002"):
        assert len(db.records_for_patient(conn, pk)) == 1
    conn.close()


# ---------------------------------------------------------------------------
# 8) Fix-B P2/P1：不覆寫既有病人 / dob 正規化
# ---------------------------------------------------------------------------


def test_resolve_existing_does_not_overwrite_dob_name(env):
    # 選既有病人只歸檔，絕不寫 patients：dob/name 保持不變。
    conn = _conn(env)
    db.upsert_patient(conn, "P-0000009", name="舊病人", dob="2008-08-08")
    conn.close()

    review_file = _fake_image(env.data_root / "review" / "keepdata.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "existing", "patient_key": "P-0000009"})
    assert resp.status_code == 303

    conn = _conn(env)
    p = db.get_patient(conn, "P-0000009")
    assert p["name"] == "舊病人"
    assert p["dob"] == "2008-08-08"
    assert len(db.records_for_patient(conn, "P-0000009")) == 1
    conn.close()


def test_resolve_new_id_existing_national_id_reuses_without_overwrite(env):
    # 建新病人但證號已建檔 → 視同選既有：不改其資料、直接歸檔，resolution 註明沿用。
    conn = _conn(env)
    db.insert_patient_strict(conn, VALID_ID, "原姓名", "2010-05-05", None, "system")
    conn.close()

    review_file = _fake_image(env.data_root / "review" / "reuse.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(
        env, client, f"/queue/{item_id}/resolve",
        {"action": "new_id", "patient_id": VALID_ID, "name": "打錯的名字", "dob": "1999-09-09"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/p/{VALID_ID}"

    conn = _conn(env)
    p = db.get_patient(conn, VALID_ID)
    assert p["name"] == "原姓名"      # 未被覆寫
    assert p["dob"] == "2010-05-05"  # 未被覆寫
    assert len(db.records_for_patient(conn, VALID_ID)) == 1
    item = conn.execute("SELECT resolution FROM queue_items WHERE id=?", (item_id,)).fetchone()
    assert "沿用既有" in item["resolution"]
    # 沒有真的新建 → 不應留 create_patient 稽核。
    cp = conn.execute(
        "SELECT 1 FROM audit WHERE action='create_patient' AND patient_key=?", (VALID_ID,)
    ).fetchone()
    conn.close()
    assert cp is None


def test_resolve_normalizes_roc_dob_into_iso(env):
    # 表單 dob 一律經 normalize_date：民國年寫法應轉成 ISO 落庫。
    review_file = _fake_image(env.data_root / "review" / "roc.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve", {"action": "new_pcode", "name": "民國生", "dob": "104/05/04"})
    assert resp.status_code == 303

    conn = _conn(env)
    p = db.get_patient(conn, "P-0000001")
    conn.close()
    assert p["dob"] == "2015-05-04"  # 民國104 → 2015


def test_resolve_bad_dob_blocked_and_not_inserted(env):
    # dob 亂格式 → 表單錯誤重填（400），病人不入庫、佇列維持 open、檔案未動。
    review_file = _fake_image(env.data_root / "review" / "bad_dob.jpg")
    item_id = _add_queue(env, "photo_batch", "首見證號", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = _post(
        env, client, f"/queue/{item_id}/resolve",
        {"action": "new_id", "patient_id": VALID_ID, "name": "王小明", "dob": "not-a-real-date"},
    )
    assert resp.status_code == 400
    assert "生日" in resp.text

    conn = _conn(env)
    assert db.get_patient(conn, VALID_ID) is None  # 未入庫
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert item["state"] == "open"
    assert review_file.exists()


# ---------------------------------------------------------------------------
# 9) Fix-B P1：CSRF
# ---------------------------------------------------------------------------


def test_resolve_missing_csrf_rejected(env):
    review_file = _fake_image(env.data_root / "review" / "nocsrf.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    # 刻意不帶 csrf。
    resp = client.post(
        f"/queue/{item_id}/resolve", data={"action": "new_pcode"}, follow_redirects=False
    )
    assert resp.status_code == 403

    conn = _conn(env)
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    rej = conn.execute("SELECT 1 FROM audit WHERE action='csrf_reject'").fetchone()
    conn.close()
    assert item["state"] == "open"     # 未認領
    assert rej is not None             # 有 audit
    assert review_file.exists()        # 未歸檔


def test_resolve_wrong_csrf_rejected(env):
    review_file = _fake_image(env.data_root / "review" / "badcsrf.jpg")
    item_id = _add_queue(env, "photo_batch", "無任何身份線索", [review_file])

    client = _login(env.app, "boss", "boss-pw")
    resp = client.post(
        f"/queue/{item_id}/resolve",
        data={"action": "new_pcode", "csrf": "not-the-real-token"}, follow_redirects=False,
    )
    assert resp.status_code == 403

    conn = _conn(env)
    item = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert item["state"] == "open"


def test_users_create_wrong_csrf_rejected(env):
    client = _login(env.app, "boss", "boss-pw")
    resp = client.post(
        "/users",
        data={"username": "nurse", "password": "pw", "role": "viewer", "csrf": "bad"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    conn = _conn(env)
    assert db.get_user(conn, "nurse") is None
    conn.close()


# ---------------------------------------------------------------------------
# 10) 帳號管理與登出
# ---------------------------------------------------------------------------


def test_manager_create_user_and_audit(env):
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, "/users", {"username": "nurse", "password": "nurse-pw", "role": "viewer"})
    assert resp.status_code == 303

    conn = _conn(env)
    user = db.get_user(conn, "nurse")
    audit = conn.execute("SELECT * FROM audit WHERE action='create_user'").fetchone()
    conn.close()
    assert user is not None and user["role"] == "viewer"
    assert auth.verify_pw("nurse-pw", user["pwhash"])
    assert audit is not None


def test_create_duplicate_user_rejected(env):
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, "/users", {"username": "boss", "password": "again", "role": "manager"})
    assert resp.status_code == 400
    assert "已存在" in resp.text


def test_manager_queue_detail_renders_with_search(env):
    review_file = _fake_image(env.data_root / "review" / "d.jpg")
    item_id = _add_queue(
        env, "photo_batch", "首見證號", [review_file],
        extracted={"ids": [VALID_ID], "names": ["王小明"], "dob": "2015-01-01"},
    )
    conn = _conn(env)
    db.upsert_patient(conn, "P-0000009", name="可搜尋的人")
    conn.close()

    client = _login(env.app, "boss", "boss-pw")
    resp = client.get(f"/queue/{item_id}", params={"q": "可搜尋"})
    assert resp.status_code == 200
    # 縮圖牆、extracted 欄位、搜尋結果、建檔動作表單、csrf 欄皆應出現。
    assert f"/queue/{item_id}/file/0" in resp.text
    assert "王小明" in resp.text
    assert "可搜尋的人" in resp.text
    assert "一鍵配 P 碼" in resp.text
    assert 'name="csrf"' in resp.text


def test_audit_page_renders(env):
    client = _login(env.app, "boss", "boss-pw")
    resp = client.get("/audit")
    assert resp.status_code == 200
    assert "稽核紀錄" in resp.text
    # 剛才的登入動作應可見。
    assert "login" in resp.text


def test_logout_clears_session(env):
    client = _login(env.app, "boss", "boss-pw")
    assert client.get("/", follow_redirects=False).status_code == 200

    _post(env, client, "/logout")
    # 登出後再取受保護頁 → 導回 /login。
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_logout_wrong_csrf_rejected(env):
    client = _login(env.app, "boss", "boss-pw")
    resp = client.post("/logout", data={"csrf": "bad"}, follow_redirects=False)
    assert resp.status_code == 403
    # session 未被清除，仍可存取。
    assert client.get("/", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# 審查 R2：card_suspect 禁一般歸檔動作／認領失敗退回佇列
# ---------------------------------------------------------------------------


def test_card_suspect_rejects_generic_filing_action(env):
    """card_suspect 用一般歸檔動作（existing）→ 400，項目維持 open、檔案不動（R2 P0）。"""
    item_id, dest, rec_id = _seed_card_item(env, "card_generic.jpg")
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "existing", "patient_key": VALID_ID})
    assert resp.status_code == 400
    assert "僅能選" in resp.text
    conn = _conn(env)
    row = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert row["state"] == "open"
    assert dest.is_file()


def test_resolve_failure_reverts_claim_to_open(env, monkeypatch):
    """認領後歸檔爆例外 → 500、項目退回 open、audit 記 resolve_error（R2 P1）。"""
    conn = _conn(env)
    db.upsert_patient(conn, VALID_ID, name="王小明", dob="2015-01-01")
    src = _fake_image(env.data_root / "review" / "boom.jpg")
    item_id = db.add_queue_item(
        conn, "photo_batch", "測試用",
        {"files": [str(src)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY},
    )
    conn.close()

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(webapp.archiver, "file_record", _boom)
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "existing", "patient_key": VALID_ID})
    assert resp.status_code == 500
    conn = _conn(env)
    row = conn.execute(
        "SELECT state, resolution FROM queue_items WHERE id=?", (item_id,)
    ).fetchone()
    audits = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE action='resolve_error'"
    ).fetchone()["n"]
    conn.close()
    assert row["state"] == "open" and row["resolution"] is None
    assert audits == 1
    assert src.is_file()  # 檔案原地未動


def test_resolve_failure_records_partial_progress(env, monkeypatch):
    """new_pcode 建了病人後歸檔失敗 → 退回 open＋payload.partial 記已建病人（R3 P0）。"""
    conn = _conn(env)
    src = _fake_image(env.data_root / "review" / "partial.jpg")
    item_id = db.add_queue_item(
        conn, "photo_batch", "測試用",
        {"files": [str(src)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY},
    )
    conn.close()

    def _boom(*args, **kwargs):
        raise RuntimeError("mid-flight failure")

    monkeypatch.setattr(webapp.archiver, "file_record", _boom)
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "new_pcode", "name": "無名氏"})
    assert resp.status_code == 500
    conn = _conn(env)
    row = conn.execute(
        "SELECT state, reason, payload FROM queue_items WHERE id=?", (item_id,)
    ).fetchone()
    created = conn.execute(
        "SELECT patient_key FROM patients WHERE patient_key LIKE 'P-%'"
    ).fetchall()
    conn.close()
    assert row["state"] == "open"
    assert "部分完成" in row["reason"] and "既有病人" in row["reason"]
    payload = json.loads(row["payload"])
    assert payload["partial"]["patient_key"] == created[0]["patient_key"]
    assert payload["partial"]["filed"] == []


def test_partial_progress_blocks_new_patient_on_retry(env):
    """payload.partial 存在時，重試用 new_pcode/new_id → 400 封鎖、不建第二位病人（R4 P0）。"""
    conn = _conn(env)
    db.insert_patient_strict(conn, "P-0000001", "無名氏", None, None, "boss")
    src = _fake_image(env.data_root / "review" / "retry.jpg")
    item_id = db.add_queue_item(
        conn, "photo_batch", "測試用",
        {"files": [str(src)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY,
         "partial": {"patient_key": "P-0000001", "filed": []}},
    )
    conn.close()
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "new_pcode", "name": "又一位"})
    assert resp.status_code == 400
    assert "已封鎖再新建" in resp.text
    conn = _conn(env)
    n = conn.execute("SELECT COUNT(*) AS n FROM patients").fetchone()["n"]
    state = conn.execute("SELECT state FROM queue_items WHERE id=?", (item_id,)).fetchone()["state"]
    conn.close()
    assert n == 1          # 沒有第二位病人
    assert state == "open"  # 未認領

    # 改用「既有病人」選 partial 記錄的代號 → 成功歸檔
    resp2 = _post(env, client, f"/queue/{item_id}/resolve",
                  data={"action": "existing", "patient_key": "P-0000001"})
    assert resp2.status_code == 303


def test_partial_with_filed_binds_retry_to_same_patient(env):
    """已實際歸檔給 P1 → existing 選 P2 也被封鎖；選 P1 放行（R5 P0）。"""
    conn = _conn(env)
    db.insert_patient_strict(conn, "P-0000001", "無名氏", None, None, "boss")
    db.insert_patient_strict(conn, "P-0000002", "另一位", None, None, "boss")
    src = _fake_image(env.data_root / "review" / "bind.jpg")
    item_id = db.add_queue_item(
        conn, "photo_batch", "測試用",
        {"files": [str(src)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY,
         "partial": {"patient_key": "P-0000001", "filed": ["/tmp/already_filed.jpg"]}},
    )
    conn.close()
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "existing", "patient_key": "P-0000002"})
    assert resp.status_code == 400
    assert "僅能" in resp.text
    resp2 = _post(env, client, f"/queue/{item_id}/resolve",
                  data={"action": "existing", "patient_key": "P-0000001"})
    assert resp2.status_code == 303


def test_ghost_partial_intent_is_voided(env):
    """partial 指向不存在的病人（write-ahead 意圖未實現）→ 作廢，new_pcode 可用（R5）。"""
    conn = _conn(env)
    src = _fake_image(env.data_root / "review" / "ghost.jpg")
    item_id = db.add_queue_item(
        conn, "photo_batch", "測試用",
        {"files": [str(src)], "extracted": {}, "batch_key": PHOTO_BATCH_KEY,
         "partial": {"patient_key": "P-9999999", "filed": []}},
    )
    conn.close()
    client = _login(env.app, "boss", "boss-pw")
    resp = _post(env, client, f"/queue/{item_id}/resolve",
                 data={"action": "new_pcode", "name": "新生兒"})
    assert resp.status_code == 303
    conn = _conn(env)
    n = conn.execute("SELECT COUNT(*) AS n FROM patients").fetchone()["n"]
    conn.close()
    assert n == 1
