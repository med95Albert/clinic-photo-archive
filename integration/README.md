# 整合層（clinic_archive）

`integration/` 是「診所照片與檢驗報告歸檔系統」的自建服務層（Python 套件
`clinic_archive`），對應 [`../docs/architecture.md`](../docs/architecture.md) 裡的
「整合層」元件。**架構與判準規則一律以 `../docs/architecture.md` 為權威文件**——
本 README 只講怎麼跑、怎麼測、目前有哪些已知限制，沒有另外定義任何規則。

## 這是什麼、跟 ClinicSnap 的關係

拍照端用的是開源工具 [ClinicSnap](https://github.com/leon80148/ClinicSnap)
（部署前提：`archiveMode=by_patient`）：同仁手機掃碼拍照、健保卡本機辨識，照片落地到
它自己輸出的 `staging/` 資料夾（見 `../docs/architecture.md` 第 3 節「元件分工」）。

**整合層與 ClinicSnap 互不 import、不共用任何程式碼**，兩者的唯一介面是檔案系統：

```
ClinicSnap（現成 release，不改碼）
    │ 寫入
    ▼
staging/{病患代碼}/{YYYY-MM-DD}_{HHMMSS}_{idx}[-{碰撞後綴}].{ext}
staging/_unsorted/{同格式}                          ← 掃不到代碼的批次
    │ clinic_archive.watcher 讀取、判準、搬移
    ▼
自動歸檔 archive/ 或 待確認佇列 review/
```

`clinic_archive/batching.py` 對這個檔名格式的假設，就是 `contract_test.py`（見下方
「契約測試」一節）要驗證的「契約」——ClinicSnap 只要沒有改變這個輸出格式，整合層就不
需要跟著改。`taiwan_id.py` 對 ClinicSnap 內部身分證抽取邏輯的參考，也只取行為（逐行
抽取、checksum），不複製、不 import 其原始碼。

## 快速開始（從原始碼跑）

目前沒有提供任何打包／安裝程式，一律以原始碼＋虛擬環境執行。以下指令在
`integration/`（本檔案所在目錄）下執行。

### 1. 建立虛擬環境並安裝

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

依賴白名單見 `pyproject.toml`：`fastapi`、`uvicorn`、`jinja2`、`python-multipart`、
`rapidocr`、`onnxruntime`、`pillow`；`[dev]` 額外安裝 `pytest`、`httpx`。需要
Python 3.11 以上。

### 2. 啟動

```bash
python run.py
```

- **建議一律用 `python run.py`**（本目錄根層）：它會把套件路徑自己插進
  `sys.path` 再啟動，不論從哪個工作目錄呼叫、也不管當初 `pip install -e ".[dev]"`
  的 editable-install 有沒有確實生效，都能正確 import 到 `clinic_archive`。
- 另一種等價方式：在 `integration/`（本目錄）內執行 `python -m clinic_archive.main`。
  這個寫法依賴 editable install 確實生效、且目前工作目錄要在對的位置，某些環境下
  會出現 `ModuleNotFoundError: No module named 'clinic_archive'`；不確定時請優先用
  `python run.py`。
- **`config.json` 與 `data_root`（預設 `clinic_data/`）一律建立在「執行當下的工作
  目錄」，不是 `run.py` 檔案或 `clinic_archive` 套件所在的位置**：即使你執行
  `python /path/to/integration/run.py`，這兩者仍會出現在你當下所在的目錄下，不會
  自動跟著 run.py 的路徑走（詳見下方「資料夾佈局」一節）。固定部署請永遠從同一個
  目錄啟動，或把 `data_root` 改成絕對路徑。
- 找不到 `config.json` 時會自動建立預設值並寫回；接著在 `data_root` 之下建立
  `staging/`、`inbox/`、`archive/`、`review/`、`trash/` 五個工作資料夾、初始化
  `clinic.db`、建立初始管理員帳號，並跑一輪**開機耐久性巡檢**（比對 `archive/`／
  `review/` 底下的實體檔與 DB／佇列索引，把上次非正常關機遺留的孤兒檔掛回佇列供
  人工複核；統計筆數寫進 `integration.log`）。
- **初始管理員密碼**寫在 `{data_root}/FIRST_RUN_ADMIN.txt`（帳號固定 `admin`，密碼明碼
  存在檔案裡）。登入後請立刻用右上角「改密碼」（`/password`，需輸入目前密碼、新密碼至少
  8 字元）換掉，再刪除這個檔案——改密後該帳號在其他裝置的登入全部失效，明碼檔即使外流也
  無法再用。同仁忘記密碼由管理員在「帳號」頁重設；管理員本人忘記時在伺服器主控台執行
  `python run.py --set-password admin`（提示輸入、不回顯、不啟動服務）。
- **log 淨化**：`integration.log` 與主控台輸出一律經 `clinic_archive/redact.py`：證號遮成
  `A12345****`、暫時代號 `P-123****`；工作資料夾（inbox/review/trash/staging/archive）底下
  非系統產生的檔名（可能含姓名）換成 `h<雜湊>.jpg`（系統檔名用精確樣式判定；引號內路徑可含空白；未加引號又含人取檔名的路徑會從該處遮到行尾），traceback 裡的路徑也一樣；網址 query 值
  一律 `<redacted>`。uvicorn 存取 log 整個關閉（誰看了什麼由稽核表負責）。
  要讀任何 log（含 ClinicSnap 的 `clinic_snap.log`）請一律經淨化器：
  `python -m clinic_archive.redact <log 路徑> --tail 200`。淨化器認得的是結構化識別碼、
  路徑與 query，自由文字裡的姓名認不出——若仍看到疑似姓名，那是 bug，請回報。
- 預設監聽 `http://0.0.0.0:8770`（可在 `config.json` 調整 `web_host`/`web_port`）。
- 停止：`Ctrl+C`（SIGINT）或送 SIGTERM，watcher 執行緒與 web 服務會一併優雅停止。
- 指定設定檔位置：`python run.py --config /path/to/config.json`（或
  `python -m clinic_archive.main --config /path/to/config.json`）。

## 設定檔（config.json）

不存在時自動建立，欄位如下（預設值、說明皆取自 `clinic_archive/config.py`）：

| 欄位 | 型別 | 預設值 | 說明 |
|---|---|---|---|
| `data_root` | str | `"./clinic_data"` | 資料根目錄；下會自動建立五個工作資料夾（見下方「資料夾佈局」） |
| `db_path` | str | `"{data_root}/clinic.db"` | SQLite 索引檔路徑；`{data_root}` 佔位符載入時展開為實際路徑 |
| `web_host` | str | `"0.0.0.0"` | 網頁介面監聽位址 |
| `web_port` | int | `8770` | 網頁介面監聽埠 |
| `settle_seconds` | int | `10` | 批次靜置窗（秒）；組內最新檔案落地未滿此秒數則本輪跳過，避免處理寫入中的半批 |
| `poll_seconds` | int | `3` | watcher 輪詢 `staging/`、`inbox/` 的間隔（秒） |
| `ocr_version` | str | `"PPOCRV6"` | OCR 模型版本，`PPOCRV6` 或 `PPOCRV5` |
| `det_side_len` | int | `960` | OCR 偵測邊長參數 |
| `session_hours` | int | `12` | 登入 session 有效時數 |
| `allowed_exts` | list[str] | `[".jpg",".jpeg",".png",".webp",".heic",".pdf"]` | `inbox/` 報告影像允許處理的副檔名；不在白名單者直接進佇列 |

`allowed_exts` 只影響 `inbox/`（檢驗報告）的處理；`staging/`（相片批）的副檔名判斷是
`clinic_archive.batching.FILE_RE` 內建的 `jpe?g|png|webp|heic`（不含 pdf），不吃這個
設定欄位。

損毀的 `config.json`（非合法 JSON、或根節點不是物件）會被備份成 `.bak` 後以預設值
重建，不會讓程式當掉。

## 資料夾佈局

`config.json` 與 `data_root`（預設 `"./clinic_data"`）都是**相對路徑**，實際落點由
執行啟動指令（`python run.py` 或 `python -m clinic_archive.main`）當下的工作目錄決定；
固定部署時建議改用絕對路徑，或永遠從同一個目錄啟動，避免每次啟動位置飄移。以預設值
為例：

```
（執行時的工作目錄）/
├── config.json                 ← 設定檔（見上表）
└── clinic_data/                ← data_root 預設值
    ├── staging/                ← ClinicSnap 寫入的相片批；watcher 讀完就搬走，不留底
    │   ├── {病患代碼}/…
    │   └── _unsorted/…
    ├── inbox/                  ← 檢驗報告影像（v0：人工從 LINE 拖入）
    ├── archive/
    │   └── {身分證號 或 P-碼}/
    │       └── {YYYY-MM-DD}_{類型}[-{子類}]_{序號:02d}.{ext}
    ├── review/                 ← 待確認佇列項目的檔案實體（保留原名，衝突加 -2/-3 後綴）
    ├── trash/                  ← 一鍵刪除的疑似證件照落腳處（可回收、非永久刪除）
    ├── clinic.db               ← SQLite 索引＋稽核（WAL mode）
    ├── integration.log         ← rotating log（啟動時建立）
    └── FIRST_RUN_ADMIN.txt     ← 首次啟動自動產生，登入後請改密並刪除
```

三個維度（病人×日期×類型）與完整歸類規則見 `../docs/architecture.md` 第 6 節。

## 測試

```bash
python -m pytest tests/ -q
```

`pyproject.toml` 已設定 `addopts = "-m 'not e2e'"`，預設會跳過標了
`@pytest.mark.e2e` 的測試（`tests/test_ocr_live.py`——需要下載 OCR 模型、跑真實推論，
CI 上通常不開）。要一併跑：

```bash
python -m pytest tests/ -q -m e2e
# 或不篩選 marker，全部一起跑：
python -m pytest tests/ -q -m ""
```

## 契約測試（contract_test.py）

`clinic_archive/batching.py` 假設 ClinicSnap 輸出固定的檔名／資料夾格式（見上方「這是
什麼」一節）。`contract_test.py`（本目錄根層，跟這份 README 同一層）獨立驗證這個假設
是否仍然成立，逐條印 PASS/FAIL＋證據，結尾一定以 `CONTRACT: PASS` 或
`CONTRACT: FAIL` 收尾：

```bash
# 對真實 ClinicSnap staging 輸出驗證
python contract_test.py /path/to/clinic_data/staging

# 不需要真實資料：自產一組模擬輸出（正常批＋碰撞後綴＋_unsorted）到暫存目錄再驗
python contract_test.py --simulate

# 額外檢查 ClinicSnap 自己的 config.json（token/saveDir/archiveMode 欄位；
# 只印警告，不影響 CONTRACT 判定——這是兩件不同的事）
python contract_test.py /path/to/clinic_data/staging --config /path/to/ClinicSnap/config.json

# 供 CI／快速自我檢查：跑 --simulate 並斷言結果為 PASS
python contract_test.py --selftest
```

**請在升級 ClinicSnap 版本前先跑一次**（對它當時的 staging 輸出跑
`python contract_test.py <staging_dir>`）：如果上游改了命名規則，這裡會先報
`CONTRACT: FAIL`，而不是等正式環境的 watcher 把整批資料判成「疑混批」進了佇列才被
發現。升版後、正式讓 watcher 接手前，建議再跑一次確認新版輸出格式沒有變。

驗證的四條契約：

1. 所有檔名可被 `FILE_RE` 解析。
2. 同一批（同資料夾＋同時間戳、且無碰撞後綴）序號恰為 1..N 連續。
3. 碰撞後綴（`-2`、`-3`…）可被正確辨識，且不出現不合理的 `-1`。
4. staging 第一層只能是「病患代碼資料夾」或 `_unsorted`（不應有落單根檔案、不應有二層以上巢狀）。

   契約④與正式管線的容忍度**刻意不同**，兩者不衝突：`batching.scan_staging` 對散落在
   staging 根層的檔案是**照常處理**的——它們被歸成「無代碼批」（`pid=None`），一樣要過
   完整判準才可能自動歸檔。所以契約④在這種情形報 FAIL，意思是「上游輸出結構跟預期不一樣，
   值得看一眼」，**屬提示、不是資料風險**（既不會遺失、也不會歸錯人）。二層以上巢狀則是
   真的會被漏看：`scan_staging` 只走一層，巢狀子資料夾裡的檔案不會被掃到。

對一份**目前正在使用中**的 staging 資料夾跑，如果看到某條 FAIL，不代表資料會遺失或
歸錯人——正式管線本身一律 fail-closed（可疑批次進佇列，絕不硬猜），這支工具只是提早
把「這個瞬間的快照裡有異常」攤開來讓人先看一眼。

## 誠實限制（v0）

- **PDF 一律進佇列**：`inbox/` 收到的 PDF 不做 OCR／文字抽取（依賴白名單內沒有 PDF
  解析套件），一律直接進佇列由人工處理（`clinic_archive/ocr.py`、`reports.py`）。
- **卡片照不自動刪**：相片批中識別出的健保卡影像不會自動刪除，會另掛一個
  `card_suspect` 佇列項目，由管理者在佇列詳情頁一鍵整批移到 `trash/`（可回收，
  非永久刪除，`webapp.py`）。
- **無縮圖快取**：時間軸與佇列頁面的圖片直接讀原檔顯示，沒有另外產生或快取縮圖，
  檔案較大或較多時載入會比較慢。
- **併檔／改鍵僅程式 API**：`clinic_archive/archiver.py` 的 `merge_patient`（併檔）與
  `rename_patient_key`（P-碼補證號）v0 只能用 Python API 呼叫，網頁介面沒有對應按鈕；
  v1 才會補上（`webapp.py`）。
- **佇列只有管理者能處理**：佇列詳情、縮圖與裁決（`/queue/…`）以及帳號管理、稽核頁都要
  `manager` 角色；`viewer` 帳號只能看總覽、病人搜尋與時間軸，點進佇列項會拿到 403。
  同仁若回報「看得到有待確認項目卻打不開」，先確認他的角色，而不是當成故障。
- **Windows 打包腳本尚未提供**：目前沒有 PyInstaller 或其他打包腳本，部署暫時一律用
  本文件「快速開始」的原始碼＋虛擬環境方式執行。
- **傳輸層安全依賴網段控制、非傳輸加密**：ClinicSnap 目前以區網明文 HTTP 傳輸；本系統
  的隱私保證建立在 `../docs/architecture.md` 第 8 節列出的網段控制（WPA3-SAE、AP
  client isolation、防火牆 ACL、伺服器走有線）**全部實測通過**的前提上，這幾項本身不
  是整合層程式碼能保證的事，部署前務必對照該節逐項驗證。

## 延伸閱讀

- [`../docs/architecture.md`](../docs/architecture.md) — 權威設計文件：總架構、身份解析
  三層防呆、N0 錨定協定、歸類模式、法遵與隱私、分階段路線。
- [`SPEC.md`](SPEC.md) — 本目錄各模組的實作契約（architecture.md 的細節落地）。
