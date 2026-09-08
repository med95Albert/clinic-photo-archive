"""歸檔搬移與資料夾重整（SPEC §9）。

負責把已通過判準的檔案原子性搬入 ``archive/{patient_key}/``、寫 records/audit，
以及佇列化檔案搬入 ``review/``、併檔（merge）、P-碼補證號改名（rename）。

fail-closed 相關不變量：
  * 搬移一律 ``os.replace``（同磁碟原子）；跨磁碟 fallback = copy+fsync+unlink。
  * 目的檔已存在 → 加 ``-2``/``-3`` 後綴（模仿上游碰撞慣例），**絕不覆蓋**。
  * DB 只在檔案確實就位後才寫入。
  * ``file_record`` 的檔名宣告＝模組級 ``threading.Lock`` ＋ ``os.open(O_CREAT|O_EXCL)``
    佔位雙保險：同一 patient/date/type 底下多執行緒併發寫入時，序號計算與目的檔
    宣告全程序列化，且「建立目的檔案」這個動作本身在作業系統層級具原子性，兩個
    呼叫者絕不可能拿到同一個目的路徑（Fix-C：修復併發覆寫＝資料遺失，已被壓測
    重現：12 併發寫入只剩 2-4 檔、sha 與檔不符）。
  * ``reconcile()`` 提供開機耐久性巡檢：比對 archive/review 實體檔與 DB／佇列索引，
    找出「搬檔後崩潰」留下的孤兒檔並掛回 ``queue_items`` 供人工複核，不自動猜測、
    不自動刪除（Fix-C：修復搬檔後崩潰＝孤兒檔）。

``cfg`` 以 duck-typing 讀取 ``cfg.data_root``（不硬 import T2 的 config.py）。
DB 一律直接參數化 SQL（不依賴 db.py 的 DAO）。
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB

# _move 對「暫時性鎖檔」的有界重試。Windows 上 Defender 即時掃描與搜尋索引器會
# 短暫開著剛落地的檔案，此時 os.replace 會拋 PermissionError（winerror 32
# ERROR_SHARING_VIOLATION）。這種鎖通常幾百毫秒內就放開，但 watcher 是輪詢迴圈：
# 不重試就會每一輪重搬、重失敗、把 log 洗爆，而檔案永遠卡在 staging。
# 有界（而非無限）重試：真正的權限問題仍要如實拋出，不能無聲吞掉。
_MOVE_RETRY_ATTEMPTS = 5
_MOVE_RETRY_BASE_SLEEP = 0.3  # 秒；第 n 次重試前睡 n * BASE（0.3/0.6/0.9/1.2）

# Windows ERROR_SHARING_VIOLATION：檔案正被其他行程開著。
_WINERROR_SHARING_VIOLATION = 32

# file_record() 的「序號計算＋目的檔名宣告」臨界區鎖。單一 Python 行程內把整段
# 「掃現有檔→算 seq→佔位」序列化；O_CREAT|O_EXCL 則是即使鎖涵蓋不到的呼叫路徑
# 也不會覆寫既有檔的最後防線。見 _claim_destination() docstring。
_seq_lock = threading.Lock()


# --------------------------------------------------------------------------
# 路徑輔助
# --------------------------------------------------------------------------
def _data_root(cfg: Any) -> Path:
    return Path(cfg.data_root)


def _archive_dir(cfg: Any) -> Path:
    return _data_root(cfg) / "archive"


def _review_dir(cfg: Any) -> Path:
    return _data_root(cfg) / "review"


def _dedup(dest: Path) -> Path:
    """目的已存在 → 依上游慣例回傳 name-2/name-3… 的第一個空位。"""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 2
    while (parent / f"{stem}-{n}{suffix}").exists():
        n += 1
    return parent / f"{stem}-{n}{suffix}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as r:
        for chunk in iter(lambda: r.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_transient_lock_error(e: OSError) -> bool:
    """這個 OSError 像不像「檔案被別人短暫開著」（值得重試）？

    Windows：Defender／索引器持有檔案時是 winerror 32（ERROR_SHARING_VIOLATION）。
    POSIX：EACCES／EPERM 也可能是暫時性（如 NFS lock），重試幾次無害。
    """
    if getattr(e, "winerror", None) == _WINERROR_SHARING_VIOLATION:
        return True
    return e.errno in (errno.EACCES, errno.EPERM)


def _replace_with_retry(src: Path, dest: Path) -> None:
    """``os.replace``，對暫時性鎖檔做有界重試。

    EXDEV 與所有非鎖檔錯誤立即往外拋（EXDEV 由 ``_move`` 接手走跨磁碟 fallback）。
    重試 ``_MOVE_RETRY_ATTEMPTS`` 次仍失敗就拋出最後一個例外——絕不無聲吞掉。
    """
    for attempt in range(1, _MOVE_RETRY_ATTEMPTS + 1):
        try:
            os.replace(src, dest)
            return
        except OSError as e:
            if e.errno == errno.EXDEV or not _is_transient_lock_error(e):
                raise
            if attempt == _MOVE_RETRY_ATTEMPTS:
                logger.warning(
                    "搬移 %s → %s 連續 %d 次被鎖住，放棄重試：%s",
                    src, dest, _MOVE_RETRY_ATTEMPTS, e,
                )
                raise
            logger.debug(
                "搬移 %s → %s 被鎖住（第 %d/%d 次），稍後重試：%s",
                src, dest, attempt, _MOVE_RETRY_ATTEMPTS, e,
            )
            time.sleep(_MOVE_RETRY_BASE_SLEEP * attempt)


def _move(src: Path, dest: Path) -> None:
    """同磁碟 os.replace；跨磁碟 copy+fsync+unlink（原子性盡力而為）。

    ``src`` 可以是檔案或目錄（``rename_patient_key`` 的整夾更名會傳目錄）。
    同磁碟時 ``os.replace`` 兩者都吃；跨磁碟時目錄必須改走 ``shutil.move``——
    原本的 fallback 會 ``open(src, "rb")``，對目錄在 Linux 上拋 IsADirectoryError、
    在 Windows 上拋 PermissionError（誤導成權限問題），等於整夾更名一旦跨磁碟就爆。
    """
    try:
        _replace_with_retry(src, dest)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise

    # 跨磁碟 fallback
    if src.is_dir():
        # shutil.move 在 dest 已存在且為目錄時會把 src「搬進去」而非取代它——
        # 那會靜默產生 archive/新key/舊key/ 這種巢狀結構。呼叫端已保證 dest 不存在
        # （rename_patient_key 只在 not new_dir.exists() 時走整夾更名），這裡再擋一次。
        if dest.exists():
            raise FileExistsError(f"跨磁碟整夾搬移的目的地已存在，拒絕合併：{dest}")
        shutil.move(str(src), str(dest))
        return

    tmp = dest.with_name(dest.name + ".part")
    with open(src, "rb") as r, open(tmp, "wb") as w:
        for chunk in iter(lambda: r.read(_CHUNK), b""):
            w.write(chunk)
        w.flush()
        os.fsync(w.fileno())
    _replace_with_retry(tmp, dest)
    os.unlink(src)


def _relocate_dir(from_dir: Path, to_dir: Path) -> dict[str, str]:
    """把 from_dir 內所有檔案搬入 to_dir（碰撞加後綴）。

    回傳 {舊絕對路徑字串: 新絕對路徑字串} 供 records.path 更新。搬完若 from_dir 空則移除。
    """
    mapping: dict[str, str] = {}
    if not from_dir.exists():
        return mapping
    to_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(from_dir.iterdir(), key=lambda p: p.name):
        if not item.is_file():
            continue
        dest = _dedup(to_dir / item.name)
        _move(item, dest)
        mapping[str(item)] = str(dest)
    try:
        from_dir.rmdir()
    except OSError:
        pass  # 尚有子項（非檔案）→ 保留
    return mapping


def _add_audit(
    conn: sqlite3.Connection, actor: str, action: str,
    patient_key: str | None, detail: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit(actor, action, patient_key, detail) VALUES(?,?,?,?)",
        (actor, action, patient_key, detail),
    )


def _claim_destination(patient_dir: Path, prefix: str, ext: str) -> Path:
    """在鎖保護下計算 seq 並以 ``O_CREAT|O_EXCL`` 原子佔位宣告目的檔名。

    Fix-C P1 併發覆寫修法：舊版是「數 existing 檔案數 → 組出 seq 檔名 → ``.exists()``
    檢查」三個各自獨立的步驟，多執行緒之間存在 TOCTOU 空隙——兩個執行緒可能都算出
    同一個 seq、都覺得目的檔不存在，先搬到的那個就被後搬到的悄悄覆蓋（``os.replace``
    對已存在的目的檔不會報錯）。此為雙保險修法：

      1) 模組級 ``_seq_lock`` 序列化本函式全程（同一 Python 行程內的並行執行緒）。
      2) 即使某條呼叫路徑漏包了鎖，``os.open(..., O_CREAT | O_EXCL)`` 本身在作業
         系統層級是原子操作：撞名必定丟 ``FileExistsError``，兩個呼叫者絕不可能
         同時「成功建立」同一個路徑。

    佔位檔建立後立刻對其他執行緒的 ``iterdir()`` 掃描可見，因此後續呼叫自然把
    seq 往後遞增，或在 seq 檔名本身也撞了既有檔（例如非本系統寫入的雜項檔案）時
    依上游慣例 bump ``-2``/``-3`` 後綴——同一函式一次處理兩層碰撞，不需要呼叫端
    另外協調。回傳值是一個已存在、大小 0 位元組、呼叫者獨占的路徑，呼叫者接著以
    ``os.replace`` 蓋上真正內容。
    """
    with _seq_lock:
        existing = [
            p for p in patient_dir.iterdir() if p.is_file() and p.name.startswith(prefix)
        ]
        seq = len(existing) + 1
        stem = f"{prefix}{seq:02d}"
        bump: int | None = None
        while True:
            name = f"{stem}{ext}" if bump is None else f"{stem}-{bump}{ext}"
            target = patient_dir / name
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                bump = 2 if bump is None else bump + 1
                continue
            os.close(fd)
            return target


def _cleanup_placeholder(dest: Path) -> None:
    """搬移／雜湊失敗時盡力刪除自己剛佔位的 0 位元組檔，避免留下垃圾佔位檔。

    只在仍是空檔（尚未被真正內容覆蓋）時才刪，避免誤刪已搬移成功的合法空檔案。
    若行程在走到這裡之前就已崩潰（例如被強制關機），佔位檔會留在 archive/ 下且
    DB 無對應 records 列——這種情況由 ``reconcile()`` 之後掃描出來當孤兒檔處理，
    不在這裡試圖處理（fail-closed：寧可留給人工複核，不臆測、不強行清理）。
    """
    try:
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
    except OSError:
        pass


def _add_queue_item(conn: sqlite3.Connection, kind: str, reason: str, payload: dict) -> int:
    """新增 queue_items 列（不依賴 T2 db.py DAO，直接參數化 SQL）。回傳新列 id。"""
    cur = conn.execute(
        "INSERT INTO queue_items(kind, reason, payload) VALUES(?,?,?)",
        (kind, reason, json.dumps(payload, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def _open_queue_payload_files(conn: sqlite3.Connection, kind: str | None = None) -> set[str]:
    """開放（state='open'）佇列項 payload 裡 ``files`` 清單的路徑聯集。

    ``kind=None`` 取全部種類；``reconcile()`` 拿它做兩種用途：
      * ``kind='orphan'``：既有的孤兒佇列項已涵蓋哪些路徑（冪等判斷，避免重複建立）。
      * ``kind=None``（review/ 掃描時）：所有開放佇列項（含 orphan 自己）宣稱擁有
        哪些檔案——這個聯集本身就順帶讓孤兒判斷冪等，不需要另外特判。
    payload 格式不符預期（非 JSON、非物件、無 files 清單）一律當作沒有貢獻路徑，
    絕不因單一髒資料列讓整個 reconcile() 掛掉。
    """
    if kind is None:
        rows = conn.execute("SELECT payload FROM queue_items WHERE state='open'").fetchall()
    else:
        rows = conn.execute(
            "SELECT payload FROM queue_items WHERE state='open' AND kind=?", (kind,)
        ).fetchall()
    paths: set[str] = set()
    for row in rows:
        payload_raw = row[0]
        try:
            payload = json.loads(payload_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        files = payload.get("files")
        if isinstance(files, list):
            paths.update(str(f) for f in files)
    return paths


# --------------------------------------------------------------------------
# 對外 API（SPEC §9）
# --------------------------------------------------------------------------
def file_record(
    conn: sqlite3.Connection,
    cfg: Any,
    src_path: Path,
    patient_key: str,
    taken_date: str,
    rtype: str,
    subtype: str | None,
    src: str,
    batch_key: str | None,
    actor: str = "system",
) -> Path:
    """搬檔入 ``archive/{patient_key}/{taken_date}_{rtype[-subtype]}_{seq:02d}{ext}``。

    seq = 該患者當日同型現有檔數 + 1；落 records（含 sha256）與 audit('auto_file')。
    回傳最終目的路徑。

    併發安全（Fix-C P1）：目的檔名的計算與宣告透過 ``_claim_destination`` 以
    Lock＋O_CREAT|O_EXCL 雙保險原子佔位，多執行緒同時對同一 patient/date/type
    寫入時彼此絕不覆蓋、也不遺失（各自拿到互斥的 seq 或 ``-2``/``-3`` 後綴）。
    """
    src_path = Path(src_path)
    label = f"{rtype}-{subtype}" if subtype else rtype
    ext = src_path.suffix.lower()

    patient_dir = _archive_dir(cfg) / patient_key
    patient_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"{taken_date}_{label}_"
    dest = _claim_destination(patient_dir, prefix, ext)

    try:
        # 先算雜湊（來源仍在），再搬移覆蓋自己剛佔位的檔，最後才寫 DB。
        sha = _sha256(src_path)
        _move(src_path, dest)
    except Exception:
        _cleanup_placeholder(dest)
        raise

    conn.execute(
        "INSERT INTO records(patient_key, taken_date, rtype, subtype, src, path, "
        "sha256, status, batch_key) VALUES(?,?,?,?,?,?,?, 'auto', ?)",
        (patient_key, taken_date, rtype, subtype, src, str(dest), sha, batch_key),
    )
    _add_audit(conn, actor, "auto_file", patient_key, dest.name)
    conn.commit()
    return dest


def claimed_move(src: Path, dest_dir: Path) -> Path:
    """把 src 以原名搬進 dest_dir：鎖＋O_EXCL 佔位、撞名 bump -2/-3、絕不覆蓋。

    與 file_record 同一把 ``_seq_lock`` 與佔位手法，供 trash 等受控搬移共用
    （審查 R2 P1：先前 trash 走「exists() 檢查＋os.replace」兩步非原子，
    兩個並發刪卡可挑中同一個目的名互相覆蓋）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = src.stem, src.suffix
    with _seq_lock:
        bump: int | None = None
        while True:
            name = f"{stem}{ext}" if bump is None else f"{stem}-{bump}{ext}"
            target = dest_dir / name
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                bump = 2 if bump is None else bump + 1
                continue
            os.close(fd)
            break
    try:
        _move(src, target)
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return target


def move_to_review(cfg: Any, src_path: Path) -> Path:
    """佇列化：把檔案搬入 ``review/``（保留原名，衝突加後綴）。不寫 DB。"""
    src_path = Path(src_path)
    review = _review_dir(cfg)
    review.mkdir(parents=True, exist_ok=True)
    dest = _dedup(review / src_path.name)
    _move(src_path, dest)
    return dest


def merge_patient(
    conn: sqlite3.Connection, cfg: Any, from_key: str, to_key: str, actor: str,
) -> None:
    """併檔：from_key 的資料夾檔案逐一搬入 to_key，records 改 key，audit('merge')。

    Fix-C：開頭先驗 to_key 確實存在於 patients，否則 raise ValueError。
    ``records.patient_key`` 有 ``REFERENCES patients(patient_key)`` 外鍵；若 to_key
    不存在，檔案會先被 ``_relocate_dir`` 實際搬移完畢，之後的
    ``UPDATE records SET patient_key=?`` 才會因違反外鍵而失敗——那時檔案已經搬走、
    DB 卻沒同步更新，造成「records.path 指向已不存在的舊路徑」的資料不一致（正是
    ``reconcile()`` 要抓的孤兒情境之一）。提前擋在檔案搬移之前，讓呼叫端在還沒有
    任何副作用時就拿到明確錯誤。
    """
    if conn.execute(
        "SELECT 1 FROM patients WHERE patient_key=?", (to_key,)
    ).fetchone() is None:
        raise ValueError(
            f"merge_patient：目標病人 to_key={to_key!r} 不存在於 patients，"
            "拒絕併檔（避免檔案搬移後 records 外鍵更新失敗、造成孤兒檔）"
        )

    from_dir = _archive_dir(cfg) / from_key
    to_dir = _archive_dir(cfg) / to_key
    mapping = _relocate_dir(from_dir, to_dir)

    for old_path, new_path in mapping.items():
        conn.execute("UPDATE records SET path=? WHERE path=?", (new_path, old_path))
    conn.execute(
        "UPDATE records SET patient_key=? WHERE patient_key=?", (to_key, from_key)
    )
    _add_audit(conn, actor, "merge", to_key, f"{from_key}->{to_key}")
    conn.commit()


def rename_patient_key(
    conn: sqlite3.Connection, cfg: Any, old_key: str, new_key: str, actor: str,
) -> None:
    """P-碼補證號：資料夾更名、records/patients 更新、audit('reassign')。"""
    old_dir = _archive_dir(cfg) / old_key
    new_dir = _archive_dir(cfg) / new_key

    # patients：以新 key 建列（沿用舊列欄位），稍後移除舊列。
    old_row = conn.execute(
        "SELECT name, dob, chart_no FROM patients WHERE patient_key=?", (old_key,)
    ).fetchone()
    if old_row is not None:
        name, dob, chart_no = old_row[0], old_row[1], old_row[2]
        exists_new = conn.execute(
            "SELECT 1 FROM patients WHERE patient_key=?", (new_key,)
        ).fetchone()
        if exists_new is None:
            conn.execute(
                "INSERT INTO patients(patient_key, name, dob, chart_no) VALUES(?,?,?,?)",
                (new_key, name, dob, chart_no),
            )

    # 檔案搬移：new_dir 不存在時整夾更名最省事，否則逐檔併入。
    if old_dir.exists() and not new_dir.exists():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        _move(old_dir, new_dir)
        # 整夾更名 → 檔名不變，僅前綴改變。
        rows = conn.execute(
            "SELECT id, path FROM records WHERE patient_key=?", (old_key,)
        ).fetchall()
        for rid, path in rows:
            new_path = str(new_dir / Path(path).name)
            conn.execute("UPDATE records SET path=? WHERE id=?", (new_path, rid))
    else:
        mapping = _relocate_dir(old_dir, new_dir)
        for old_path, new_path in mapping.items():
            conn.execute("UPDATE records SET path=? WHERE path=?", (new_path, old_path))

    conn.execute(
        "UPDATE records SET patient_key=? WHERE patient_key=?", (new_key, old_key)
    )
    conn.execute("DELETE FROM patients WHERE patient_key=?", (old_key,))
    _add_audit(conn, actor, "reassign", new_key, f"{old_key}->{new_key}")
    conn.commit()


# --------------------------------------------------------------------------
# 開機耐久性巡檢（Fix-C P1：搬檔後崩潰＝孤兒檔）
# --------------------------------------------------------------------------
def reconcile(conn: sqlite3.Connection, cfg: Any) -> dict:
    """比對 archive/、review/ 實體檔與 DB／佇列索引，把孤兒檔掛回 queue_items。

    三種孤兒情境（皆 fail-closed：只回報、不臆測、不自動搬移或刪除）：
      1. archive/ 下有實體檔，但 records 表找不到對應 path 的列
         → queue_item(kind='orphan', reason='歸檔區發現無索引檔案')。
         典型成因：``file_record`` 在 ``_move`` 完成、DB commit 之前行程被中止
         （例如斷電、被強制關機），檔案已就位但索引沒寫進去。
      2. records 表的 path 指向的檔案在磁碟上不存在
         → queue_item(kind='orphan', reason='索引指向的檔案遺失')。
         典型成因：檔案被檔案系統層面的操作（人工手動搬移/刪除、備份還原不全）
         動過，DB 索引沒有跟著更新。
      3. review/ 下有實體檔，但所有「開放中」佇列項的 payload.files 聯集都沒有
         提到它 → queue_item(kind='orphan', reason='待確認區發現無主檔案')。
         典型成因：``move_to_review`` 搬檔成功後、``add_queue_item`` 寫入前
         行程被中止，檔案落地了但沒有佇列項認領它。

    冪等：呼叫前已存在的 open 孤兒佇列項所涵蓋的路徑不會重複建立（每次開機重跑
    都只回報「這次新發現」的孤兒，不會每次都把舊的孤兒項再灌一次）。

    回傳計數 dict：``{"archive_orphan_files", "archive_missing_records",
    "review_orphan_files"}``。呼叫端（main.py bootstrap）負責 log 統計。
    """
    counts = {
        "archive_orphan_files": 0,
        "archive_missing_records": 0,
        "review_orphan_files": 0,
    }

    # 冪等基準：既有 open 的孤兒項已經涵蓋哪些路徑。
    already_orphaned = _open_queue_payload_files(conn, kind="orphan")

    # ---- 1) + 2)：archive/ 實體檔 vs records.path（雙向比對）--------------
    adir = _archive_dir(cfg)
    disk_paths = {str(p) for p in adir.rglob("*") if p.is_file()} if adir.exists() else set()
    db_rows = conn.execute("SELECT id, patient_key, path FROM records").fetchall()
    db_paths = {row[2] for row in db_rows}

    for path in sorted(disk_paths - db_paths):
        if path in already_orphaned:
            continue
        _add_queue_item(conn, "orphan", "歸檔區發現無索引檔案", {"files": [path]})
        counts["archive_orphan_files"] += 1

    for rid, pkey, path in db_rows:
        if path in disk_paths or path in already_orphaned:
            continue
        _add_queue_item(
            conn,
            "orphan",
            "索引指向的檔案遺失",
            {"files": [path], "record_id": rid, "patient_key": pkey},
        )
        counts["archive_missing_records"] += 1

    # ---- 3) 先做 佇列卡死回收：認領後行程中止留下的 resolved/'processing' 殭屍項 ----
    # 開機時不可能有在途請求，一律退回 open（審查 R2 P1）。必須在 review 掃描**之前**做：否則殭屍項引用的檔案會先被誤判無主、
    # 掛成 orphan，退回後同一檔案就有兩個 open 項可被分別歸到不同病人（審查 R3 P0）。
    cur = conn.execute(
        "UPDATE queue_items SET state='open', resolved_by=NULL, resolved_at=NULL, "
        "resolution=NULL WHERE state='resolved' AND resolution='processing'"
    )
    counts["stale_processing_reverted"] = cur.rowcount


    # ---- 4) review/ 實體檔 vs 所有開放佇列項 payload.files 聯集 -----------
    # kind=None：含 orphan 自己，讓「已掛過孤兒項的路徑」自然被視為已被引用，
    # 冪等不需要另外特判。
    rdir = _review_dir(cfg)
    review_disk = {str(p) for p in rdir.rglob("*") if p.is_file()} if rdir.exists() else set()
    referenced = _open_queue_payload_files(conn, kind=None)

    for path in sorted(review_disk - referenced):
        _add_queue_item(conn, "orphan", "待確認區發現無主檔案", {"files": [path]})
        counts["review_orphan_files"] += 1

    conn.commit()
    return counts
