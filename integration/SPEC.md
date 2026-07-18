# 整合層 v0 — 實作規格（SPEC）

> 本檔是 `docs/architecture.md`（已經 5 輪跨模型審查定案）的**實作契約**。凡與 architecture.md 衝突之處，以 architecture.md 為準並回報，不得自行放寬。執行者只實作被指派的檔案，不改其他檔案。

## 0. 全域約定

- Python 3.11+，跨平台（開發 macOS、部署 Windows）；一律 `pathlib`，禁止寫死絕對路徑與 `\` 字串拼接。
- 依賴白名單：stdlib、`fastapi`、`uvicorn`、`jinja2`、`python-multipart`、`rapidocr`、`onnxruntime`、`pillow`。測試另可用 `pytest`、`httpx`。**不得**新增其他依賴。
- 密碼雜湊用 `hashlib.scrypt`（stdlib）；資料庫用 `sqlite3`（stdlib，WAL mode）。
- UI 與使用者可見字串一律繁體中文；log 用 `logging`（rotating，`integration.log`）。
- 所有「搬檔」動作：同磁碟用 `os.replace`；目的檔已存在時加 `-2`、`-3` 後綴（模仿上游慣例）；搬移前確保目的資料夾存在。
- **fail-closed 鐵律**：任何解析失敗、狀態不明、判準不滿足 → 進佇列，絕不猜、絕不刪、絕不覆蓋。
- 佇列化的檔案實體移到 `review/` 下（保留原名，衝突加後綴），佇列項記錄其現位置。

## 1. 目錄與檔案分工

```
integration/
  pyproject.toml            ← 已由指揮者建立
  clinic_archive/
    __init__.py             ← 空
    config.py               ← T2
    db.py                   ← T2
    auth.py                 ← T2
    taiwan_id.py            ← T1
    ocr.py                  ← T3
    extract.py              ← T3（純文字欄位抽取，不碰 OCR 引擎）
    batching.py             ← T4
    predicate.py            ← T4
    archiver.py             ← T4
    reports.py              ← T5
    watcher.py              ← T5
    main.py                 ← T5
    webapp.py               ← T6
    templates/…             ← T6（jinja2）
  tests/
    test_taiwan_id.py       ← T1
    test_config_db_auth.py  ← T2
    test_extract.py         ← T3
    test_batching.py        ← T4
    test_predicate.py       ← T4
    test_archiver.py        ← T4
    test_pipeline_e2e.py    ← T5（模擬 ClinicSnap 寫檔行為，不需真 OCR）
    test_ocr_live.py        ← T3（pytest.mark.e2e，需下載模型，CI 可跳過）
    test_webapp.py          ← T6（fastapi TestClient）
  contract_test.py          ← T7
  README.md                 ← T7
```

## 2. config.py（T2）

`AppConfig` dataclass ＋ `load_config(path) -> AppConfig`（JSON，不存在則建預設並寫回；損毀則備份 `.bak` 重建）。欄位（含預設）：

```
data_root: str = "./clinic_data"        # 之下自動建 staging/ inbox/ archive/ review/ trash/
db_path: str = "{data_root}/clinic.db"
web_host: str = "0.0.0.0"
web_port: int = 8770
settle_seconds: int = 10
poll_seconds: int = 3
ocr_version: str = "PPOCRV6"            # PPOCRV6|PPOCRV5
det_side_len: int = 960
session_hours: int = 12
allowed_exts: [".jpg",".jpeg",".png",".webp",".heic",".pdf"]
```

`ensure_dirs(cfg)` 建立五個子資料夾。`{data_root}` 佔位符在 load 時展開。

## 3. db.py（T2）

`connect(db_path)` → sqlite3 連線（WAL、foreign_keys=ON、Row factory）。`init_db(conn)` 執行 DDL（idempotent）。DAO 一律用參數化查詢。DDL：

```sql
CREATE TABLE IF NOT EXISTS patients(
  patient_key TEXT PRIMARY KEY,        -- 身分證號 或 P-0000123
  name TEXT, dob TEXT,                 -- dob ISO YYYY-MM-DD，可 NULL
  chart_no TEXT,                       -- HIS 病歷號別名，可 NULL
  created_by TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_key TEXT NOT NULL REFERENCES patients(patient_key),
  taken_date TEXT NOT NULL,            -- YYYY-MM-DD
  rtype TEXT NOT NULL,                 -- 病灶照|檢驗|InBody|文件|其他
  subtype TEXT,                        -- CBC|生化|尿液|過敏原|…
  src TEXT NOT NULL,                   -- phone|inbox|import
  path TEXT NOT NULL, sha256 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'auto', -- auto|confirmed
  batch_key TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS processed_batches(
  batch_key TEXT PRIMARY KEY,          -- "{pid|~}|{YYYY-MM-DD}|{HHMMSS}"
  state TEXT NOT NULL,                 -- auto_filed|queued
  processed_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS queue_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                  -- photo_batch|report|straggler|card_suspect
  reason TEXT NOT NULL,
  payload TEXT NOT NULL,               -- JSON：{files:[…], extracted:{…}, batch_key}
  state TEXT NOT NULL DEFAULT 'open',  -- open|resolved
  resolution TEXT, resolved_by TEXT, resolved_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, pwhash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','manager')),
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
  expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  actor TEXT NOT NULL,                 -- 帳號 或 'system'
  action TEXT NOT NULL,                -- login|view_timeline|view_file|auto_file|queue|resolve|merge|reassign|create_user|delete_card
  patient_key TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_records_patient ON records(patient_key, taken_date);
```

DAO 函式（皆 `(conn, …)`）：`upsert_patient`、`get_patient`、`find_patient_by_chart`、`insert_record`、`records_for_patient`、`mark_batch`、`batch_state`、`add_queue_item`、`open_queue_items`、`resolve_queue_item`、`add_audit`、`create_user`、`get_user`、`create_session`、`get_session`（自動過期刪除）、`list_patients(search)`。

## 4. auth.py（T2）

- `hash_pw(pw)` / `verify_pw(pw, stored)`：scrypt n=2**14 r=8 p=1，格式 `scrypt$<salt_hex>$<hash_hex>`。
- `ensure_initial_admin(conn, data_root)`：users 空 → 建 `admin`＋`secrets.token_urlsafe(9)` 密碼，寫入 `{data_root}/FIRST_RUN_ADMIN.txt`（提示登入後改密與刪檔）並 log。
- `new_session(conn, username, hours)` → token（`secrets.token_urlsafe(32)`）；`check_session(conn, token)` → username|None。

## 5. taiwan_id.py（T1）

公開 API（簽名固定，T3/T4 依此 import）：

```python
LETTER_VALUES: dict[str,int]   # 內政部字母對照（A=10…Z=33，含 I=34,O=35）
verify_checksum(pid: str) -> bool          # [A-Z][0-9]{9} 加權和 %10==0
extract_ids(text: str) -> list[str]        # 逐行、去雜訊、去重、保序；含新式統一證號
extract_old_resident_ids(text: str) -> list[str]   # [A-Z]{2}[0-9]{8}，無 checksum
is_pcode(key: str) -> bool                 # ^P-\d{7}$
next_pcode(conn) -> str                    # 查 patients 取最大 P- 流水，回下一號
classify_manual_input(s: str) -> tuple[str, str]
# 回 (kind, value)：kind ∈ 'national_id'（格式符且 checksum 過）| 'pcode' | 'chart_no'（其他英數）| 'invalid'
```

實作參考（僅參考行為，不複製整檔）：`../../../ClinicSnap/src/clinic_snap/services/patient_id_ocr.py` 的逐行抽取與 checksum。測試至少：已知有效/無效號、單碼錯（權重×誤差≡0 mod 10 的漏網例也要有——用它證明「checksum 非唯一防線」的註解誠實）、跨行黏字、混雜全形。

## 6. ocr.py ＋ extract.py（T3）

**ocr.py**（薄引擎層）：
```python
get_engine(ocr_version, det_side_len)      # lru_cache；rapidocr 統一套件，v6→medium、v5→mobile
ocr_image_text(path_or_bytes, cfg) -> str  # EXIF 校正→RGB→引擎→逐行文字；threading.Lock 序列化
ocr_pdf_text(path, cfg) -> str             # v0：PDF 先嘗試 pypdf?（不在白名單）→ 改為：不支援 PDF OCR，
                                           # 但 PDF 有文字層時用「不新增依賴」的方式抽不可行 → v0 規則：
                                           # PDF 一律進佇列（reason='PDF 需人工'），記在 README 限制
```
（PDF 抽取列 v1；v0 只收影像檔，PDF 直接佇列——誠實限制，不硬做。）

**extract.py**（純文字，不碰引擎，完整單元測試）：
```python
extract_report_fields(text) -> ReportFields
# ReportFields: ids:list[str], names:list[str], dob:str|None(ISO), chart_no:str|None, report_date:str|None(ISO), keywords:set[str]
normalize_date(s) -> str|None   # 支援 2019-05-04、2019/5/4、114/05/04、090.05.04、民國90年5月4日 → ISO；民國年=西元-1911
detect_card(text) -> bool       # 含「全民健康保險」或「健保卡」字樣
classify_report(keywords) -> tuple[str, str|None]   # rtype, subtype
# 規則：含 CBC|血球|血紅素→('檢驗','CBC')；生化|AST|ALT|肌酸酐→('檢驗','生化')；尿液→('檢驗','尿液')；
# IgE|過敏原→('檢驗','過敏原')；InBody|體脂→('InBody',None)；其他有「檢驗|報告」字樣→('檢驗',None)；都沒有→('文件',None)
```
姓名抽取：`姓\s*名[:：]?\s*([一-鿿]{2,4})`；dob 找「出生|生日」行；report_date 找「報告日|採檢日」行。

## 7. batching.py（T4）——照 architecture.md §5.6，一字不放寬

ClinicSnap `by_patient` 輸出：`staging/{pid}/{YYYY-MM-DD}_{HHMMSS}_{idx}[-{coll}].{ext}`；無 ID 批在 `staging/_unsorted/` 同格式。

```python
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_(\d+)(?:-(\d+))?\.(jpe?g|png|webp|heic)$", re.I)
@dataclass BatchGroup: key:str; pid:str|None; date:str; time:str; files:list[Path]; complete:bool; suspect_reason:str|None
scan_staging(staging: Path, now=None) -> list[BatchGroup]
# 1) 走訪一層子資料夾（資料夾名=pid；_unsorted→pid=None）＋容忍直接散落檔（pid=None）
# 2) 檔名不合 FILE_RE → 單獨成組 suspect_reason='檔名格式不明'
# 3) 依 (pid, date, time) 分組成 batch_key = f"{pid or '~'}|{date}|{time}"
# 4) 靜置窗：組內最新 mtime 距 now < settle_seconds → 本輪跳過（不回傳）
# 5) 完整性：idx 集合必須恰為 1..N 且無 coll 後綴；否則 suspect_reason='序號不連續或碰撞後綴＝疑混批'
# 6) 已處理鍵（由呼叫端查 processed_batches 後傳入或回傳後過濾）：鍵已存在 → 整組標 suspect_reason='遲到檔'
```
測試：正常批、亂序寫入未靜置、碰撞後綴、缺號、遲到檔、_unsorted、垃圾檔名，各斷言分組與 suspect_reason。

## 8. predicate.py（T4）——architecture.md §4 判準，單一實作點

```python
@dataclass Verdict: auto_file:bool; patient_key:str|None; reason:str
def decide_photo_batch(conn, group:BatchGroup, ocr_ids:list[str], card_dob:str|None) -> Verdict
def decide_report(conn, fields:ReportFields) -> Verdict
```
photo 分支邏輯（依序）：
1. suspect_reason 非空 → queue(reason)。
2. 解析 pid：`classify_manual_input`；pcode→患者需存在；chart_no→`find_patient_by_chart`；national_id→驗 checksum。解析失敗/查無 → queue。
3. 候選證號 = set(ocr_ids 通過 checksum)。**恰一**：len>1 → queue('批內多個證號')。
4. len==1：id=候選。未建檔 → queue('首見證號')。已建檔：
   - card_dob 可讀：與 patients.dob 吻合 → auto；不吻合 → queue('生日不符')。
   - card_dob 不可讀：pid 解析出的病人 == id 的病人 → auto（手機端人工背書）；否則 queue。
5. len==0：無卡批。pid 解析到已建檔病人 → **queue('無卡批需人工一鍵確認')**（§4：無複掃證號可比對，fail closed）；pid 也無 → queue('無任何身份線索')。
報告分支：恰一 checksum 證號→已建檔→**dob 必須可讀且吻合**→auto；其餘 queue（首見證號、生日缺/不符、多證號、零證號各給明確 reason）。
測試：真值表逐格；特別含「有效證號但未建檔」「證號吻合但生日不符」「無卡但 pid=已建檔」三個關鍵 fail-closed 例。

## 9. archiver.py（T4）

```python
file_record(conn, cfg, src_path, patient_key, taken_date, rtype, subtype, src, batch_key, actor='system') -> Path
# archive/{patient_key}/{taken_date}_{rtype[-subtype]}_{seq:02d}{ext}；seq=該患者當日同型現有數+1
# sha256 落 records；audit('auto_file'…)；os.replace 搬移；跨磁碟 fallback copy+fsync+unlink
move_to_review(cfg, src_path) -> Path
merge_patient(conn, cfg, from_key, to_key, actor)   # 資料夾檔案逐一搬併＋records 改 key＋audit('merge')
rename_patient_key(conn, cfg, old_key, new_key, actor)  # P-碼補證號：資料夾更名＋records/patients 更新＋audit
```
測試用 tmp_path 實體驗證：搬移原子性（來源消失、目的存在）、重名後綴、seq 遞增、merge 後 DB 一致。

## 10. reports.py ＋ watcher.py ＋ main.py（T5）

- `process_inbox(conn, cfg)`：掃 inbox 檔案（mtime 靜置 settle 秒）；副檔名不在白名單或 .pdf → move_to_review＋queue('PDF 需人工'/'格式不支援')；影像 → `ocr_image_text`→`extract_report_fields`→`decide_report`→ auto 則 `file_record`（taken_date=report_date or 檔案 mtime 日）／queue 則 move_to_review＋queue_item（payload 含 extracted 供 UI 顯示）。
- `process_staging(conn, cfg)`：`scan_staging`→查 processed_batches 標遲到→逐組：對每張圖 `ocr_image_text`→收集 ids（`extract_ids` 過 checksum）＋`detect_card` 的圖記 card 候選＋card_dob（對 detect_card 的圖跑 `extract_report_fields().dob`）→`decide_photo_batch`→ auto：逐檔 `file_record(rtype='病灶照')`；detect_card 且批內檔數≥2 的圖改 rtype='識別影像' 並**另建 queue_item(kind='card_suspect')** 供管理者一鍵刪除（v0 不自動刪，README 註明與 docs 差異）／queue：整組 move_to_review＋queue_item→`mark_batch`。
- `watcher_loop(cfg, stop_event)`：每 poll_seconds 跑兩個 process_*，例外 log 不中斷。
- `main.py`：載 config→ensure_dirs→init_db→ensure_initial_admin→啟 watcher thread→uvicorn 起 webapp（同 process）；SIGINT 優雅停。
- `test_pipeline_e2e.py`：**monkeypatch ocr_image_text**（回預存文字，不需真引擎）；模擬 ClinicSnap 寫檔（同時間戳批、碰撞後綴批、無卡批）→跑一輪 process→斷言 archive/review/DB/queue 全符合預期。

## 11. webapp.py ＋ templates（T6）

FastAPI＋jinja2，路由（除 /login 外全部要 session；角色標註）：

```
GET  /login、POST /login（失敗 log＋audit）
POST /logout
GET  /            → 佇列總覽（open queue_items 分 kind 列表）             viewer+
GET  /queue/{id}  → 佇列詳情：縮圖（authenticated file route）、extracted 欄位、
                    動作表單（指定病人：搜尋既有/建新檔含 P-碼、確認歸檔、整批刪除〔僅 card_suspect〕） manager
POST /queue/{id}/resolve → 依表單執行：建/選 patient → file_record 逐檔 → resolve_queue_item → audit
GET  /patients?q= → 病人搜尋（key/name/chart_no LIKE）                    viewer+
GET  /p/{key}     → 時間軸：records 依日分組、縮圖、audit('view_timeline')  viewer+
GET  /file/{record_id} → 送檔（audit('view_file')；只准 records 內路徑）    viewer+
GET  /review-file?path= 禁止——一律經 queue 詳情的受控路由（防路徑穿越）
GET  /users、POST /users → 建帳號                                        manager
GET  /audit       → 最近 500 筆                                          manager
```

模板繁中、手機優先、無外部資源（自帶 <style>，風格樸素即可）；縮圖直接 <img> 原檔（v0 不做縮圖快取）。TestClient 測試：未登入 302、viewer 禁 manager 路由、queue resolve 全流程（用 tmp 環境＋假檔）、file 路由拒絕任意 path。

## 12. contract_test.py ＋ README.md（T7）

- `contract_test.py <staging_dir>`：對真實 ClinicSnap 輸出（或 `--simulate` 自產樣本）驗四契約：檔名 regex 全數可解析、同批共用時間戳、序號 1..N、碰撞後綴格式；另 `--config <ClinicSnap config.json>` 時比對 token 欄位存在。結尾自報 `CONTRACT: PASS|FAIL`＋逐條結果。
- README：如何從原始碼跑（venv、pip、`python -m clinic_archive.main`）、config 說明、首次 admin 密碼位置、**誠實限制**（PDF 進佇列、卡片照人工刪、Windows 打包腳本未在本機驗證、無縮圖快取）、契約測試用法、與 ClinicSnap 的關係（資料夾介面、不 import）。

## 13. Done Criteria（每張工單）

1. 指派檔案存在、`python -m pytest tests/<你的測試> -q` 全綠（e2e 標記除外）。
2. 不改他人檔案、不加白名單外依賴、不留 TODO 空殼。
3. 回報：實際指令輸出（測試結果原文）、關鍵設計取捨一句話、已知限制。
