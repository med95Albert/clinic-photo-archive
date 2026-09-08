# 整合層 v0 — 實作規格（SPEC）

> 本檔是 `docs/architecture.md`（已經 5 輪跨模型審查定案）的**實作契約**。凡與 architecture.md 衝突之處，以 architecture.md 為準並回報，不得自行放寬。執行者只實作被指派的檔案，不改其他檔案。

## 0. 全域約定

- Python 3.11+，跨平台（開發 macOS、部署 Windows）；一律 `pathlib`，禁止寫死絕對路徑與 `\` 字串拼接。
- 依賴白名單：stdlib、`fastapi`、`uvicorn`、`jinja2`、`python-multipart`、`rapidocr`、`onnxruntime`、`pillow`。測試另可用 `pytest`、`httpx`。**不得**新增其他依賴。
- 密碼雜湊用 `hashlib.scrypt`（stdlib）；資料庫用 `sqlite3`（stdlib，WAL mode）。
- UI 與使用者可見字串一律繁體中文；log 用 `logging`（rotating，`integration.log`）。**所有 log／主控台／契約測試輸出一律經 `redact.py` 淨化**，三層機械規則：①證號 `A12345****`、暫時代號 `P-123****`；②`inbox/ review/ trash/ staging/ archive/` 之後的路徑段只保留系統產生的名字（`YYYY-MM-DD_HHMMSS_n[-k].ext`、日期資料夾、`_unsorted`、已遮罩識別碼），其餘（使用者取的檔名，可能含姓名）換成 `h<8 碼雜湊><副檔名>`，traceback 內路徑同樣適用；系統檔名以**精確樣式**判定（ASCII 類別，不用 `\w`——它含中文），引號內路徑吃到引號為止（可含空白、支援 Windows repr 雙反斜線），未加引號且含使用者取名的路徑因邊界不可判定 → 從該路徑起遮到行尾；③網址 query 值一律 `<redacted>`（`/patients?q=` 就是姓名）。`main._setup_logging` 掛 `redact.MaskingFormatter`；`uvicorn.run(..., log_config=None, access_log=False)`——存取 log 整個不寫（誰看了什麼由 audit 表負責）；直接印路徑／檔名／批次鍵的站點另以 `redact.mask_text`／`mask_segment`／`safe_name` 顯式淨化（雙保險）。`python -m clinic_archive.redact <log> --tail N` 是現場 agent 讀任何 log（含上游 `clinic_snap.log`）的唯一通道。誠實界線：淨化器只認得結構化的識別碼、路徑與 query，自由文字裡的姓名認不出——所以 log 站點**不得**直接印姓名或使用者輸入。
- 所有「搬檔」動作：同磁碟用 `os.replace`；目的檔已存在時加 `-2`、`-3` 後綴（模仿上游慣例）；搬移前確保目的資料夾存在。
- **fail-closed 鐵律**：任何解析失敗、狀態不明、判準不滿足 → 進佇列，絕不猜、絕不刪、絕不覆蓋。
- 佇列化的檔案實體移到 `review/` 下（保留原名，衝突加後綴），佇列項記錄其現位置。

## 1. 目錄與檔案分工

```
integration/
  pyproject.toml            ← 已由指揮者建立
  requirements.lock         ← 部署鎖版依賴：deploy/AGENT_DEPLOY.md Step 2 的**唯一依賴真相**
                              （診所機一律 `pip install -r requirements.lock` ＋
                              `pip install -e . --no-deps`；缺檔即停下回報，不得退回
                              `pip install -e ".[dev]"` 抓未鎖版依賴）
  run.py                    ← 啟動器（Fix-C）：把 `integration/` 插進 sys.path 最前面再呼叫
                              `clinic_archive.main.main()`，不依賴 editable install 是否生效；
                              不改變 config.json／data_root 的「相對於工作目錄」語意
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
    test_provenance.py      ← Fix-A 回歸：證據同源（跨圖湊吻合攻擊）、病歷號重號、
                              卡片統一處理（單張純卡批也產 card_suspect）
    test_durability.py      ← Fix-C 回歸：file_record 併發覆寫、reconcile 孤兒回收、
                              merge_patient 外鍵前置檢查、run.py 啟動器與 bootstrap 接線
  contract_test.py          ← T7
  README.md                 ← T7
```

`test_provenance.py`、`test_durability.py`、`test_webapp.py` 與 `test_predicate.py`／
`test_batching.py`／`test_archiver.py`／`contract_test.py --selftest` 同屬部署硬閘門
（見 `deploy/AGENT_DEPLOY.md` Step 3）：任一 failed 一律停下回報，不得改程式碼求綠。

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

`connect(db_path)` → sqlite3 連線（WAL、foreign_keys=ON、Row factory、`busy_timeout=10000`——watcher 執行緒與 web 請求併發寫入時先自旋等待，不立刻拋 `OperationalError`）。`init_db(conn)` 執行 DDL（idempotent）。DAO 一律用參數化查詢。

**schema 的事實來源是 `clinic_archive/db.py` 的 `SCHEMA_SQL`**，本節與它同步維護（改任一邊都要同時改另一邊）：

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
  expires_at TEXT NOT NULL,
  csrf TEXT);                          -- 每個 session 一枚 CSRF token（見第 11 節）
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  actor TEXT NOT NULL,                 -- 帳號 或 'system'
  action TEXT NOT NULL,                -- login|view_timeline|view_file|auto_file|queue|resolve|merge|reassign|create_user|delete_card
  patient_key TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_records_patient ON records(patient_key, taken_date);
```

DAO 函式（皆 `(conn, …)`）：`upsert_patient`、`insert_patient_strict`、`get_patient`、`find_patients_by_chart`、`find_patient_by_chart`、`insert_record`、`records_for_patient`、`mark_batch`、`batch_state`、`add_queue_item`、`open_queue_items`、`resolve_queue_item`、`add_audit`、`create_user`、`get_user`、`create_session`、`get_session`（自動過期刪除）、`list_patients(search)`。

DAO 語意注意（與判準安全直接相關，不得簡化）：

- `insert_patient_strict(conn, patient_key, name, dob, chart_no, created_by)`：純 `INSERT`，
  鍵已存在時放行 `sqlite3.IntegrityError` 往上拋。**人工建新檔（佇列裁決）一律走它**，
  讓呼叫端明確面對「這個鍵已經有人了」；`upsert_patient` 遇既有鍵會靜默更新，只適合系統回填。
- `find_patients_by_chart(conn, chart_no) -> list[Row]`：回**全部**命中列。病歷號在 patients
  表無唯一約束（HIS 別名可能重號），故歸檔判準一律用它並自行檢查 `len==1`，其餘 fail-closed
  進佇列。`find_patient_by_chart(conn, chart_no) -> Row|None` 是「回第一筆」的相容 wrapper，
  **僅供 UI 顯示等非歸檔用途**，判準不得使用。
- `create_session(conn, token, username, hours) -> str`：**回傳本次配發的 csrf token**
  （`secrets.token_urlsafe(16)`，與 session 同列持久化）；`get_session` 會一併帶回該欄。
- `init_db` 對舊庫（round-1 schema，sessions 無 csrf 欄）以 `PRAGMA table_info` 檢查後
  `ALTER TABLE sessions ADD COLUMN csrf TEXT` 補欄——`CREATE TABLE IF NOT EXISTS` 不會補欄，
  少了這段升級後的既有資料庫會登不進去。
- `insert_record`、`resolve_queue_item` **非 production 主路徑**：實際歸檔一律走
  `archiver.file_record`（它自己組 SQL 並在檔案就位後才寫 records＋audit），佇列裁決一律走
  `webapp.queue_resolve` 的原子認領 UPDATE（見第 11 節）。這兩個 DAO 保留給測試與工具腳本，
  新程式碼不得改走它們繞過上述不變量。

## 4. auth.py（T2）

- `hash_pw(pw)` / `verify_pw(pw, stored)`：scrypt n=2**14 r=8 p=1，格式 `scrypt$<salt_hex>$<hash_hex>`。
- `ensure_initial_admin(conn, data_root)`：users 空 → 建 `admin`＋`secrets.token_urlsafe(9)` 密碼，寫入 `{data_root}/FIRST_RUN_ADMIN.txt`（提示登入後改密與刪檔）並 log。該檔權限**盡力**收到「只有目前使用者可讀」：POSIX 走 `os.chmod(0o600)`；Windows 上 `os.chmod` 對 ACL 是 no-op，改以 `icacls /inheritance:r /grant:r <user>:R` 收緊。收緊失敗（icacls 不存在／逾時／非 NTFS／權限不足）只記警告，**不中斷首次啟動**——「權限收不緊就完全開不了機」比留一個權限較寬的檔案更糟；Windows 上另 log 一則「讀完立即刪除」警告。
- `new_session(conn, username, hours)` → token（`secrets.token_urlsafe(32)`）；`check_session(conn, token)` → username|None。
- `MIN_PASSWORD_LEN = 8`；`set_password(conn, username, new_pw, keep_token=None)`：長度不足 → `ValueError`、帳號不存在 → `LookupError`；成功＝`db.update_password` ＋ `db.delete_sessions_for_user(keep_token=…)`——**密碼一換，該帳號其他 session 立即失效**（否則刪除 `FIRST_RUN_ADMIN.txt` 只是心理安慰）。web 自助改密（`/password`）、管理員重設（`/users/reset`）、主控台 `main.set_password_cli`（`run.py --set-password USER`，`getpass` 兩次、不回顯、不啟動服務、資料庫不存在即拒絕）三條路都走它。

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
ocr_pdf_text(path, cfg) -> str             # v0 不支援，刻意 fail-loud：一律 raise NotImplementedError。
                                           # 依賴白名單無 pypdf/pdfplumber，掃描型 PDF 另需 render→OCR 依賴；
                                           # 正確路徑是 process_inbox 在呼叫 OCR 之前就把 PDF 攔下進佇列
                                           #（reason='PDF 需人工'）。不得改成靜默回空字串——那會讓 PDF 被
                                           # 當成「無文字」放行，違反 fail-closed。
```
（PDF 抽取列 v1；v0 只收影像檔，PDF 直接佇列——誠實限制，不硬做，見 README。）

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

## 7. batching.py（T4）——照 architecture.md §5 第 7 條（批次還原機制），一字不放寬

ClinicSnap `by_patient` 輸出：`staging/{pid}/{YYYY-MM-DD}_{HHMMSS}_{idx}[-{coll}].{ext}`；無 ID 批在 `staging/_unsorted/` 同格式。

```python
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_(\d+)(?:-(\d+))?\.(jpe?g|png|webp|heic)$", re.I)
@dataclass BatchGroup: key:str; pid:str|None; date:str; time:str; files:list[Path]; complete:bool; suspect_reason:str|None
scan_staging(staging: Path, now=None, *, settle_seconds: int = 10,
             processed_keys: set[str] | None = None) -> list[BatchGroup]
# 0) 名稱以 "." 開頭者一律略過（.DS_Store 等系統雜項，非臨床影像）——第一層項目與
#    資料夾內檔案都適用；contract_test.py 的走訪行為與此對齊
# 1) 走訪一層子資料夾（資料夾名=pid；_unsorted→pid=None）＋容忍直接散落檔（pid=None）
# 2) 檔名不合 FILE_RE → 單獨成組 suspect_reason='檔名格式不明'，key=f"{pid or '~'}|badname|{檔名}"
# 3) 依 (pid, date, time) 分組成 batch_key = f"{pid or '~'}|{date}|{time}"
# 4) 靜置窗：組內最新 mtime 距 now < settle_seconds → 本輪跳過（不回傳）；now 預設 time.time()，
#    測試可注入。settle_seconds 由呼叫端傳入（reports.py 傳 cfg.settle_seconds），預設 10
# 5) 完整性：idx 集合必須恰為 1..N 且無 coll 後綴；否則 suspect_reason='序號不連續或碰撞後綴＝疑混批'
# 6) processed_keys（由呼叫端查 processed_batches 後以關鍵字參數傳入）：鍵已存在 →
#    整組標 suspect_reason='遲到檔'
# 精度順序（同時成立時取最確定者）：遲到檔 > 序號/碰撞 > 乾淨
```
測試：正常批、亂序寫入未靜置、碰撞後綴、缺號、遲到檔、_unsorted、垃圾檔名、dotfile 略過，各斷言分組與 suspect_reason。

## 8. predicate.py（T4）——architecture.md §4 判準，單一實作點

```python
@dataclass Verdict: auto_file:bool; patient_key:str|None; reason:str

@dataclass ImageEvidence:            # 批內「一張圖」的獨立證據，逐圖保存、不彙總跨圖證據
    path: Path                       # 該圖路徑（歸檔／佇列時據以搬移）
    ids: list[str] = []              # 該圖 OCR 出的證號「格式層」候選（checksum 由本模組套用）
    is_card: bool = False            # 該圖是否偵測為健保卡（extract.detect_card）
    dob: str | None = None           # 該圖讀到的生日（ISO）；只採信 is_card 圖上的生日

def decide_photo_batch(conn, group: BatchGroup, images: list[ImageEvidence]) -> Verdict
def decide_report(conn, fields: ReportFields) -> Verdict
```

> **禁止回到全批 ids 聯集＋單一 `card_dob` 的舊介面**（`decide_photo_batch(conn, group, ocr_ids, card_dob)`）。
> 那個介面把全批證據攤平成兩個純量，判準無從得知「證號」與「生日」是否出自同一張圖，
> 於是 A 圖的證號可以和 B 圖的卡面生日跨圖湊成一筆假吻合而**整批歸錯人**——這是本專案
> 已被回歸測試釘住的 P0 歸錯人漏洞（`tests/test_provenance.py` 第 1 案）。任何「簡化參數」
> 的重構若丟掉逐圖結構，等於重新引入該漏洞，一律不得放行。

`images` 由 `reports._ocr_batch` 依 `group.files` 的順序（檔名序號序）逐張建立，故
`images[0]` 即 N0 位置。photo 分支邏輯（依序，任何一格不滿足即 fail-closed 進佇列）：

1. `group.suspect_reason` 非空（序號/碰撞/遲到/檔名）→ queue(該原因)。
2. 解析手機端帶入代碼 `group.pid`（若有）：`classify_manual_input`；
   `national_id`→格式符且 checksum 過即取其值（是否已建檔留待第 4 步）；
   `pcode`→患者需存在，否則 queue('暫時代號查無此人')；
   `chart_no`→`find_patients_by_chart`，**命中數恰為 1** 才可據以歸檔，0 筆或重號一律
   queue('病歷號對應不唯一或不存在')；`invalid`→queue('手機端身份代碼無法解析')。
3. 候選證號 = 全批各圖 `ids` 的**格式層聯集**（去重，**不先濾 checksum**）。**恰一原則**
   （architecture §4 第 1 條）：len>1 → queue('批內多個證號')。未過檢查碼的候選也算一個——
   跨模型審查 2026-09-09 實證：先濾再數會讓「兩人同框、其中一位證號被誤讀一碼」被當恰一而歸給
   另一位。代價是形如字母＋9 位數的檢體／報告編號會讓報告進佇列（刻意 fail-closed）。
4. len==1（cid＝該證號）：**先驗 checksum**，不過 → queue('證號未通過檢查碼')；未建檔 →
   queue('首見證號')。已建檔則依序：
   - **批內第二張（含以後）出現任何卡片影像** → queue('卡片非首張或多卡影像需人工')。
     此檢查**無條件優先**於下面所有放行分支：即使首張卡驗證全數通過，後方的卡仍可能屬於
     另一位病人（A 卡首張全吻合＋B 卡在後 → 整批誤歸 A）。
   - 首張是錨點卡（`images[0].is_card` 且 `cid in images[0].ids`）**且該圖讀得出生日**：
     建檔資料無生日 → queue('建檔資料缺生日，無法交叉核對')；生日吻合 → **auto**
     ('證號已建檔＋卡面生日吻合')；不吻合 → queue('生日不符')。
     生日必須與證號**出自同一張圖**（同圖同源），跨圖組合一律不採信。
   - 首張是錨點卡但生日不可讀：第 2 步解析出的 `resolved_key == cid`（手機端人工背書，
     與 OCR 無關的獨立驗證）→ **auto**('證號已建檔＋手機端人工背書一致')；
     否則 queue('卡面生日不可讀且手機端代碼未背書')。
   - 批內有卡但 cid 不出自首張錨點卡（證號來自文件照等）→ queue('證據不同源需人工')。
   - 批內完全沒有卡片影像（證號只見於非卡圖）→ queue('證號僅見於非卡片影像需人工')。
5. len==0（無卡批）：`group.pid` 有值（能走到這一步，代表它在第 2 步已解析成功）→
   queue('無卡批需人工一鍵確認')
   （§4：無複掃證號可比對，fail closed）；連 pid 都沒有 → queue('無任何身份線索')。

報告分支（`decide_report`）：零證號 → queue('報告無有效證號')；多證號（格式層計數，同上）→
queue('報告內多個證號')；恰一但檢查碼不過 → queue('報告證號未通過檢查碼')；恰一但未建檔 →
queue('首見證號')；**報告必有生日、生日必查**——`fields.dob` 缺 →
queue('報告缺生日')，建檔資料缺生日 → queue('建檔資料缺生日，無法交叉核對')，吻合 → **auto**
('報告證號已建檔＋生日吻合')，不吻合 → queue('生日不符')。

測試：真值表逐格；特別含「有效證號但未建檔」「證號吻合但生日不符」「無卡但 pid=已建檔」
三個關鍵 fail-closed 例，以及 `tests/test_provenance.py` 的跨圖湊吻合、後方多卡、病歷號重號三案。

## 9. archiver.py（T4）

```python
file_record(conn, cfg, src_path, patient_key, taken_date, rtype, subtype, src, batch_key, actor='system') -> Path
# archive/{patient_key}/{taken_date}_{rtype[-subtype]}_{seq:02d}{ext}；seq=該患者當日同型現有數+1
# 順序固定：先算 sha256（來源仍在）→ 搬移 → 才寫 records＋audit('auto_file')；
# 搬移／雜湊失敗即清掉自己的 0 位元組佔位檔並往上拋，DB 不留半筆
move_to_review(cfg, src_path) -> Path
claimed_move(src, dest_dir) -> Path
merge_patient(conn, cfg, from_key, to_key, actor)   # 資料夾檔案逐一搬併＋records 改 key＋audit('merge')
rename_patient_key(conn, cfg, old_key, new_key, actor)  # P-碼補證號：資料夾更名＋records/patients 更新＋audit
reconcile(conn, cfg) -> dict                        # 開機耐久性巡檢（main.bootstrap 呼叫）
```

搬移與命名的不變量：

- `_move(src, dest)`：同磁碟 `os.replace`；跨磁碟（`EXDEV`）fallback = copy＋fsync＋`os.replace`＋unlink。
  `src` 可以是檔案或**目錄**（`rename_patient_key` 的整夾更名會傳目錄）：同磁碟時 `os.replace` 兩者都吃，
  跨磁碟時目錄改走 `shutil.move`，且 `dest` 已存在即 `raise FileExistsError`——`shutil.move` 對已存在的
  目錄是「搬進去」而非取代，會靜默產生 `archive/新key/舊key/` 巢狀結構。
- `_replace_with_retry(src, dest)`：所有 `os.replace` 都走它。Windows 上 Defender／索引器短暫持有檔案
  握把會丟 winerror 32（`ERROR_SHARING_VIOLATION`）、POSIX 上 NFS lock 可能丟 EACCES/EPERM——這類
  **暫時性鎖檔**做有界重試（5 次，睡 0.3×n 秒遞增），仍失敗就拋最後一個例外並記 warning，**絕不無聲吞掉**
  （fail-closed：呼叫端清佔位檔、不寫 DB，檔案留在原處等下一輪）。`EXDEV` 與其他錯誤一律立刻往外拋。
- `_claim_destination(patient_dir, prefix, ext)`：`file_record` 的目的檔名**計算與宣告**必須在
  模組級 `threading.Lock` 保護下、以 `os.open(O_CREAT|O_EXCL)` 原子佔位完成（雙保險）。
  舊寫法「數檔案→組 seq→`.exists()` 檢查」三步之間有 TOCTOU 空隙，兩個執行緒會算出同一個 seq、
  各自覺得目的不存在，後搬的靜默覆蓋先搬的＝**資料遺失**（壓測重現：12 併發只剩 2–4 檔）。
  撞名時同一函式一次處理兩層碰撞：seq 往後遞增、seq 檔名本身也撞則 bump `-2`/`-3`。
- `claimed_move(src, dest_dir)`：以**同一把鎖與同一套 O_EXCL 佔位**把檔案以原名搬進指定資料夾
  （撞名 bump `-2`/`-3`，絕不覆蓋），供 `trash/` 等受控搬移共用（`webapp._move_to_trash`）。
  先前 trash 走「`exists()` 檢查＋`os.replace`」兩步非原子，兩個並發刪卡可挑中同一個目的名互相覆蓋。
- `merge_patient` 開頭先驗 `to_key` 存在於 patients，否則 `raise ValueError`：否則檔案會先被實際搬完，
  之後的 `UPDATE records SET patient_key=?` 才因外鍵失敗，留下「records.path 指向已不存在的舊路徑」。

`reconcile(conn, cfg)` — 開機耐久性巡檢，只回報、不臆測、不自動搬移或刪除，冪等（已被 open
孤兒項涵蓋的路徑不重複建立）。**執行順序固定**：

1. `archive/` 實體檔 vs `records.path` 雙向比對 → 前者多出來的掛
   queue_item(kind='orphan', '歸檔區發現無索引檔案')；後者指向的檔案不在磁碟上則掛
   '索引指向的檔案遺失'。
2. **先退殭屍**：把 `state='resolved' AND resolution='processing'` 的認領全部退回 `open`
   （開機時不可能有在途請求）。
3. **再掃 review/**：`review/` 實體檔 vs **所有** open 佇列項 `payload.files` 聯集，無人認領者掛
   '待確認區發現無主檔案'。
   順序不可對調：殭屍項引用的檔案若在退回前先被掃成孤兒，退回後同一檔案會有兩個 open 項，
   可被分別歸給不同病人（P0）。

回傳計數 `{"archive_orphan_files", "archive_missing_records", "stale_processing_reverted",
"review_orphan_files"}`，由 `main.bootstrap` 寫進 `integration.log`。

測試用 tmp_path 實體驗證：搬移原子性（來源消失、目的存在）、重名後綴、seq 遞增、merge 後 DB 一致、
併發 `file_record` 不覆寫（`tests/test_durability.py`）。

## 10. reports.py ＋ watcher.py ＋ main.py（T5）

- `process_inbox(conn, cfg, now=None)`：掃 inbox 檔案（mtime 靜置 `cfg.settle_seconds` 秒；dotfile 與非檔案略過）；
  `.pdf` → move_to_review＋queue('PDF 需人工')；副檔名不在 `cfg.allowed_exts` → move_to_review＋queue('格式不支援')；
  影像 → `ocr_image_text`→`extract_report_fields`→`decide_report`（**任何例外 fail-closed**：
  queue('OCR 失敗需人工')）→ auto 則 `classify_report` 定 (rtype, subtype) 後 `file_record`
  （taken_date = report_date or 檔案 mtime 日）／queue 則 move_to_review＋queue_item。
  佇列 payload 的 `extracted` 由 `_fields_payload(fields)` 產生，**必須帶 rtype／subtype**
  （與 ids／names／dob／chart_no／report_date／keywords 並列）：`webapp._filing_params` 對
  report／straggler 是讀 `extracted.rtype/subtype` 決定歸檔型別的，少了這兩欄，經佇列人工歸檔的
  檢驗報告會統一退化成「文件」，同一位病人的報告在自動與人工兩條路徑上分類不一致。
- `process_staging(conn, cfg, now=None)`：查 `processed_batches` 取已處理鍵集合傳入
  `scan_staging`（判遲到檔）→逐組：
  - 可疑批（`suspect_reason` 非空）**免 OCR**直接交判準（避開對非影像垃圾檔硬解）；
  - 其餘逐張 `ocr_image_text` 建 `ImageEvidence(path, ids=taiwan_id.extract_ids(text),
    is_card=detect_card(text), dob=<僅 is_card 圖跑 extract_report_fields().dob>)`；
    **生日只對卡圖抽取**，非卡圖的雜訊日期不採信（避免被拿去跨圖湊吻合）；OCR 例外 →
    整組 queue('OCR 失敗需人工')；
  - `decide_photo_batch(conn, group, images)` → auto：逐檔 `file_record(rtype='病灶照', subtype=None,
    src='phone', batch_key=group.key)`／queue：整組 move_to_review＋queue_item
    （kind：遲到檔→`straggler`，其餘→`photo_batch`）；兩條路徑最後都 `mark_batch`
    （`auto_filed`／`queued`）以支援遲到檔偵測。
- **card_suspect 語意（v0 實況）**：`architecture.md` 的 §5 之 5.8 與 §6 表格已同步記載本條——
  「純卡片照用畢即刪」是未來的目標形態，不是 v0 行為。auto 分支中偵測到卡的圖**一律歸為 `病灶照`**（不自動標『識別影像』、
  不自動刪除），並且**不論批內張數**（含單張純卡批）都另建
  `queue_item(kind='card_suspect', reason='健保卡影像建議人工刪除')`，payload 帶
  `{files:[歸檔後路徑], record_ids:[…], patient_key, batch_key}`，由管理者在佇列詳情頁二選一：
  `card_keep`（卡＋病灶同框 N0，檔案與紀錄不動）或 `delete_card`（純卡片照移入 `trash/`，
  可回收、非永久刪除）。
- `watcher_loop(cfg, stop_event, poll_seconds=None)`：本執行緒**自開自的 sqlite 連線**
  （連線不可跨執行緒共用，絕不與 web 端共用），每 `poll_seconds`（預設 `cfg.poll_seconds`，
  測試可縮短）跑一輪 `run_once`＝兩個 process_*；單輪任何例外只記 log、不中斷迴圈。
- `main.py`：`bootstrap()` = 載 config→ensure_dirs→**設定 log（必須最先，否則後面的統計沒有
  handler 可寫）**→init_db→ensure_initial_admin→`archiver.reconcile()` 開機耐久性巡檢並把
  孤兒統計寫進 log；`run()` 再啟 watcher thread→uvicorn 起 webapp（同 process）；
  SIGINT/SIGTERM 優雅停，uvicorn 返回後一併收束 watcher。
- `test_pipeline_e2e.py`：**monkeypatch ocr_image_text**（回預存文字，不需真引擎）；模擬 ClinicSnap 寫檔（同時間戳批、碰撞後綴批、無卡批）→跑一輪 process→斷言 archive/review/DB/queue 全符合預期。

## 11. webapp.py ＋ templates（T6）

FastAPI＋jinja2，路由（除 /login 外全部要 session；角色標註）：

```
GET  /login、POST /login（失敗 log＋audit）
POST /logout
GET  /            → 佇列總覽（open queue_items 分 kind 列表）             viewer+
GET  /queue/{id}  → 佇列詳情：縮圖（authenticated file route）、extracted 欄位、
                    動作表單（指定病人：搜尋既有/建新檔含 P-碼、確認歸檔、整批刪除〔僅 card_suspect〕） manager
GET  /queue/{id}/file/{n} → 佇列縮圖：n 僅為 payload["files"] 索引（路徑非使用者可控），
                    resolve 後須落在 data_root 之下；寫 audit('view_file')            manager
POST /queue/{id}/resolve → 依表單執行：建/選 patient → file_record 逐檔 → resolve_queue_item → audit
GET  /patients?q= → 病人搜尋（key/name/chart_no LIKE）                    viewer+
GET  /p/{key}     → 時間軸：records 依日分組、縮圖、audit('view_timeline')  viewer+
GET  /file/{record_id} → 送檔（audit('view_file')；只准 records 內路徑）    viewer+
GET  /review-file?path= 禁止——一律經 queue 詳情的受控路由（防路徑穿越）
GET  /users、POST /users → 建帳號                                        manager
POST /users/reset → 管理員重設任一帳號密碼（該帳號 session 全失效；重設自己則保留本 session）manager
GET  /password、POST /password → 自助改密（驗目前密碼、新密碼 ≥8、兩次一致；其他裝置 session 失效） viewer+
GET  /audit       → 最近 500 筆                                          manager
```

**安全與交易一致性不變量（皆為已修補的 P0／P1，回歸測試在 `tests/test_webapp.py`、
`tests/test_durability.py`；重構不得移除任何一條）**：

- **CSRF**：每個 session 綁一枚 csrf token（`db.create_session` 配發、持久化於 `sessions.csrf`、
  `current_user` 一併帶出）。GET 頁把它塞進表單 hidden 欄，**每個 POST 路由進入時**先以
  `secrets.compare_digest` 比對（`_verify_csrf`；`/logout` 內含等價比對），不符即寫
  audit('csrf_reject')＋回 403。`compare_digest` 對兩個空字串會回 True，故必須先確認
  expected 與表單值皆非空再比。登入表單（尚無 session）是唯一例外。
- **原子認領（防重放）**：真正改動狀態前先做一次
  `UPDATE queue_items SET state='resolved', resolution='processing' WHERE id=? AND state='open'`；
  `rowcount != 1` 代表已被別的請求認領 → 回「此項已被處理」友善 200 頁，**不做任何歸檔**。
  輸入驗證刻意排在認領**之前**（驗不過就重填表單、不認領、佇列維持 open）；認領後任何一步失敗
  一律 `_revert_claim_if_processing` 退回 open，不讓項目卡在 resolved/processing 永久消失。
- **部分完成 write-ahead**：病人一建立／每歸一檔就立刻把進度寫進 `payload.partial`
  （`{patient_key, filed:[…]}`），因為硬崩潰（斷電、kill -9）不會走 except handler，
  `partial` 是唯一能告訴下一位操作者「已建了誰、歸了幾檔」的憑據。據此的閘門：
  有 `partial.patient_key` → **封鎖 `new_id`／`new_pcode`**（避免建出第二位幽靈病人）；
  `partial.filed` 非空 → 只准 `action='existing'` 且 `patient_key` **等於** `partial.patient_key`
  （硬綁定續歸同一位）；`partial.patient_key` 指向的病人在 patients 表中查無（崩潰在 insert 之前）
  → 該**幽靈意圖作廢**，不得把操作者鎖死在一個不存在的代號上；P 碼撞號時以哨兵值明確清除意圖。
- **card_suspect 只准 `card_keep`／`delete_card`**：其 payload 指向**已歸檔**的檔案，走一般歸檔動作
  會把 N0 搬進另一位病人資料夾、留下指向空路徑的原 record；反向亦然（非 card_suspect 項不得用
  這兩個動作）。
- **`_load_open_item` 只取 `state='open'`**：GET 詳情／縮圖對已 resolved 的項目一律 404，
  不讓已歸檔項再被當可處理對象顯示。POST resolve 不走它，改由上述原子認領判定。
- **受控送檔**：`/file/{record_id}` 只送 records 表登記的 path；該路由與 `/queue/{id}/file/{n}`
  都必須 resolve 後確認落在 `data_root` 之下（防路徑穿越），且**兩者都寫 audit('view_file')**——
  佇列縮圖同樣是病人影像的查閱行為，稽核範圍不得只涵蓋時間軸那一條路徑。
- 歸檔前先驗檔案存在：缺檔記進 audit 與 UI 提示，其餘照歸，**不得靜默跳過**；
  人工確認的紀錄在 `file_record` 之後補 `UPDATE records SET status='confirmed'`。

模板繁中、手機優先、無外部資源（自帶 <style>，風格樸素即可）；縮圖直接 <img> 原檔（v0 不做縮圖快取）。TestClient 測試：未登入 302、viewer 禁 manager 路由、queue resolve 全流程（用 tmp 環境＋假檔）、file 路由拒絕任意 path、CSRF 缺漏／不符回 403、重放 resolve 只生效一次、部分完成後重試被導回「既有病人」。

## 12. contract_test.py ＋ README.md（T7）

- `contract_test.py <staging_dir>`：對真實 ClinicSnap 輸出（或 `--simulate` 自產樣本）驗四契約：
  ① 所有檔名可被 `FILE_RE` 解析；② 同批（同資料夾＋同時間戳、無碰撞後綴）序號恰為 1..N 連續；
  ③ 碰撞後綴可辨識且不出現不合理的 `-1`；④ staging 第一層只有病患代碼資料夾或 `_unsorted`
  （落單根檔案、二層以上巢狀皆判 FAIL）。另 `--config <ClinicSnap config.json>` 時檢查
  token/saveDir/archiveMode 欄位，**只印 WARN、不影響 CONTRACT 判定**（那是另一件事）。
  結尾自報 `CONTRACT: PASS|FAIL`＋逐條結果；`--selftest` 供 CI 快速自我檢查。
  注意契約④與 `batching.scan_staging` 的容忍度刻意不同：後者把落單根檔案當「無代碼批」
  （pid=None）照常處理，契約④報 FAIL 是「上游輸出結構不合預期」的提示，不代表資料會遺失或歸錯人。
- README：如何從原始碼跑（venv、pip、`python -m clinic_archive.main`）、config 說明、首次 admin 密碼位置、**誠實限制**（PDF 進佇列、卡片照人工刪、Windows 打包腳本未在本機驗證、無縮圖快取）、契約測試用法、與 ClinicSnap 的關係（資料夾介面、不 import）。

## 13. Done Criteria（每張工單）

1. 指派檔案存在、`python -m pytest tests/<你的測試> -q` 全綠（e2e 標記除外）。
2. 不改他人檔案、不加白名單外依賴、不留 TODO 空殼。
3. 回報：實際指令輸出（測試結果原文）、關鍵設計取捨一句話、已知限制。
