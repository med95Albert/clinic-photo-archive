# AGENT_DEPLOY — 給診所伺服器上的 Claude Code 的部署 runbook

> 讀者是**在診所 Windows 伺服器上執行的 AI agent（Claude Code）**。你的任務：把本 repo 的
> 「診所照片與檢驗報告歸檔系統」完整部署到這台機器並通過驗收。人類（醫師 Albert 或受託
> 同仁）在旁邊，只負責點需要人手的確認框與實體作業。
>
> 權威設計文件：`../docs/architecture.md`（尤其 §3 部署拓撲、§8 法遵與網路控制）。
> 本 runbook 與其衝突時，以 architecture.md 為準並回報。

## 目標與成功定義

兩個服務在本機常駐並通過第 6 步全部驗收：

1. **ClinicSnap**（上游官方 exe，不改碼）：手機拍照 → 寫入 `C:\ClinicArchive\clinic_data\staging\`
2. **整合層**（本 repo `integration/`）：watcher＋OCR＋歸檔＋佇列/時間軸網頁（port 8770）

## 鐵律（先讀完再動手）

1. **每一步做完必須驗證通過才能進下一步**；驗證失敗 → 先診斷，同法失敗兩次就停下來，把狀況整理給人類。
2. **fail-closed**：任何不確定（路徑、既有檔案、防毒攔截）→ 停下問人，不猜、不硬繞。
3. **絕不讀取病人資料入對話**：`clinic_data\` 底下的 `archive\`、`review\`、`staging\`、`inbox\`、`trash\` 內容一律不 cat/type/開檔；`clinic.db` 不 SELECT 病人列。**任何 log 只准經本機淨化器讀**：在 `C:\ClinicArchive\repo\integration` 執行 `.\.venv\Scripts\python -m clinic_archive.redact <log 路徑> --tail 200`——它會遮證號、把工作資料夾下非系統產生的檔名換成雜湊、把網址 query 值換成 `<redacted>`。`integration.log`、ClinicSnap 的 `clinic_snap.log`、任何歷史 log 一律走這條；**不得** `Get-Content`／`type`／`Select-String` 直讀原檔（過濾錯誤行不是淨化）。`contract_test.py` 的輸出在來源端已做同樣淨化，可以貼。淨化器只認得結構化的識別碼、路徑與 query，自由文字裡的姓名認不出——若淨化後仍看到疑似姓名，停止並回報為 bug；這是最後防線，不是主要控制。
4. **密碼不落對話**：`FIRST_RUN_ADMIN.txt` 的內容不得印出——只告訴人類檔案路徑，請他自己開。
5. **不越界**：不碰 HIS、不裝清單外軟體、不改系統安全設定（防火牆規則除外，且要人類看到並同意）、不啟用 ClinicSnap 的網際網路（tunnel）模式。
6. 需要系統管理員權限的步驟（防火牆、winget 裝軟體），先明講再請人類允許 UAC。
7. 全程用 PowerShell；路徑一律絕對路徑。

## 固定路徑（全文件通用）

```
C:\ClinicArchive\                 ← 一切的家（工作目錄）
C:\ClinicArchive\repo\            ← 本 repo clone 於此
C:\ClinicArchive\config.json      ← 整合層設定（Step 5b 在首次啟動前預寫，data_root 用絕對路徑）
C:\ClinicArchive\clinic_data\     ← 整合層資料（首次啟動自動建）
C:\ClinicSnap\                    ← ClinicSnap 解壓於此
C:\ClinicSnap\…\config.json       ← ClinicSnap 設定，固定與 ClinicSnap.exe 同層（Step 4 的 $exeDir）
```

> 兩個 `config.json` 是**不同的東西、不同格式**，別搞混：`C:\ClinicArchive\config.json` 是整合層的
> （snake_case 欄位，見 `integration/clinic_archive/config.py`）；`$exeDir\config.json` 是 ClinicSnap 的
> （camelCase 欄位）。兩者都必須以 **ASCII／無 BOM** 寫入，否則會被各自的程式判為損毀而重建成預設值。

## Step 1｜前置檢查

```powershell
[System.Environment]::OSVersion.Version           # Windows 10+；主版本 >= 10
$PSVersionTable.PSVersion                         # 診所機通常是 5.1；本 runbook 所有指令皆相容 PS 5.1
(Get-PSDrive C).Free/1GB                          # 建議 >= 200GB，理想 1TB
py -0p                                            # 列出已裝的 Python 與實體路徑；要看到 -3.12（或 -3.11）
py -3.12 --version                                # 需 3.11+；沒有 → winget install -e --id Python.Python.3.12（需 UAC，先告知人類）
git --version                                     # 沒有 → winget install -e --id Git.Git；或改用「下載 repo zip」路線
ping -n 1 github.com                              # 對外網路（下載依賴與模型要用）
```

> **⚠️ Windows 的 `python` 陷阱（必讀，不照做會白忙一場）**
>
> Windows 10/11 內建「應用程式執行別名（App Execution Alias）」，在 PATH 裡放了 `python.exe`／
> `python3.exe` 兩個**存根（stub）**。真的裝了 Python 時它們常常還是排在前面，於是：
> - `python --version` 可能**沒有輸出、直接跳出 Microsoft Store**，或印出跟你剛裝的那套無關的版本；
> - `python -m venv .venv` 會建出一個指向錯誤直譯器（甚至根本建不起來）的 venv，症狀要到 Step 2／3 才爆。
>
> 因此本 runbook **一律不用 `python`，改用官方啟動器 `py -3.12`**。並且請人類現在就去關掉存根：
> **設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名 → 把 `python.exe`、`python3.exe` 兩個開關關掉**。
> （這是使用者層設定、不需要 UAC，也不算改系統安全設定。）
>
> 若連 `py -0p` 都失敗或跳 Store，代表 Python 啟動器沒裝好：請人類重跑
> `winget install -e --id Python.Python.3.12`，安裝時勾選 **py launcher**，然後開新視窗重試。

裝完 Python/Git 後**開新的 PowerShell 視窗**再繼續（PATH 才會生效）。
另外問人類一句：「這台機器的固定 IP（DHCP 保留）設好了嗎？」——沒設不擋安裝，但要記進最後的人類清單。

## Step 2｜取得 repo 與整合層環境

```powershell
New-Item -ItemType Directory -Force C:\ClinicArchive | Out-Null
Set-Location C:\ClinicArchive
git clone https://github.com/med95Albert/clinic-photo-archive.git repo   # 已存在則 git -C repo pull --ff-only
Set-Location C:\ClinicArchive\repo\integration
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.lock       # 鎖版依賴（含 pytest/httpx），先裝
.\.venv\Scripts\pip install -e . --no-deps             # 再把本套件掛成 editable，不讓 pip 重解依賴
```

`integration\requirements.lock` 是**唯一的依賴真相**：它把 onnxruntime／rapidocr／fastapi 等
版本全部釘死，避免上游改版讓診所機跟開發機裝到不同東西（OCR 結果會不一樣）。
**若 `requirements.lock` 不存在** → 停下來回報人類，**不要**退回 `pip install -e ".[dev]"`
（那會抓到未鎖版的最新依賴，違反鐵律 2 fail-closed）。

**驗證**（兩行都要過）：

```powershell
.\.venv\Scripts\python -c "import fastapi, rapidocr, onnxruntime, PIL; print('deps OK')"   # 印出 deps OK
.\.venv\Scripts\python -m pytest --version                                                  # 印出 pytest 版本（Step 3 要用）
```

## Step 3｜整合層測試（在這台機器上跑一次）

```powershell
Set-Location C:\ClinicArchive\repo\integration
.\.venv\Scripts\python -m pytest tests -q          # 期望全綠
.\.venv\Scripts\python -m pytest tests\test_ocr_live.py -m e2e -q   # 首次會下載 OCR 模型（數十 MB），必須 2 passed
.\.venv\Scripts\python contract_test.py --selftest  # 結尾必須 CONTRACT: PASS
```

**判斷規則**：

1. **硬閘門清單**（任一 `failed` 一律停下回報，不得繼續部署、不得改程式碼求綠）：
   `test_ocr_live`、`contract_test.py --selftest`、`test_predicate`、`test_batching`、
   `test_archiver`、`test_provenance`、`test_durability`、`test_webapp`。
   （`test_durability` 與 `test_webapp` 承載 CSRF／防重放／部分完成／孤兒回收等 P0 修補，
   它們紅了代表網頁與交易一致性有洞，比外觀問題嚴重得多。）
2. **`test_ocr_live` 出現 `skipped` 一律視為 FAIL，直接停下**。skip 幾乎都代表機器上找不到中文字型，
   也就是**模型與推論根本沒跑**——等於這一關完全沒驗到。必須看到 `2 passed`；看到
   `2 skipped`、`1 passed 1 skipped` 都不算過。
3. **預期全綠**。Windows 上不適用的權限位元測試程式已會自動 `skipped`，所以不該再有
   「環境差異造成的失敗」這種東西。**任何 `failed` 都要看、都要回報**，不要自己判定成
   「Windows 環境差異」放過去。
4. 不論如何**不要為了讓測試變綠去改程式碼**；把 pytest 輸出原文貼進完成報告給 Albert 裁決。

## Step 4｜安裝 ClinicSnap（上游官方 release）

```powershell
# PS 5.1 預設可能只談 TLS1.0，GitHub 會直接斷線；且進度條在非互動視窗會讓下載慢十倍。
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$zip = "$env:TEMP\ClinicSnap.zip"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/leon80148/ClinicSnap/releases/download/v0.1.2/ClinicSnap-0.1.2-win64.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath C:\ClinicSnap -Force

# 找出 exe 實際所在資料夾（zip 可能多包一層），並把它記進 $exeDir 供後續每一步使用。
$exeDir = (Get-ChildItem C:\ClinicSnap -Recurse -Filter ClinicSnap.exe | Select-Object -First 1).DirectoryName
if (-not $exeDir) { throw "找不到 ClinicSnap.exe：解壓失敗、或防毒已把 exe 隔離。停下來問人類。" }
Write-Host "exeDir = $exeDir"
```

> **⚠️ `$exeDir` 是 PowerShell 工作階段變數，關掉視窗就沒了。**
> Step 4 之後（含 Step 5 的捷徑、Step 6 的驗收）每一個用到 `$exeDir` 的指令，都必須在**同一個
> 視窗**、而且在上面那段賦值**之後**執行。若中途換了新視窗，**先把上面 `$exeDir = (Get-ChildItem …)`
> 那兩行重跑一次**再繼續。
> 為什麼要這麼囉嗦：`$exeDir` 若是空的，PowerShell 不會報錯，`"$exeDir\config.json"` 會靜靜變成
> `\config.json`（寫到磁碟根目錄）——ClinicSnap 讀不到設定、用預設值重建、saveDir 接線靜默失效，
> **而且底下用同一個空變數做的 `Select-String`／`Test-Path` 驗證還會一起假 PASS**，你會以為全過了。

預寫 ClinicSnap 設定（**接線關鍵**：saveDir 指向整合層的 staging；歸檔模式 by_patient；區網模式）：

```powershell
if (-not $exeDir) { throw '$exeDir 未設定：請先回到上一格重新偵測 ClinicSnap.exe 位置，不要繼續。' }
$snapCfg = Join-Path $exeDir "config.json"

$token = & C:\ClinicArchive\repo\integration\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(16))"
if (-not $token) { throw 'token 產生失敗（venv python 沒回東西）：不要寫入空 token，先修好 Step 2 的 venv。' }
# 不要印出 $token（鐵律 4：密碼／token 不落對話）。
@"
{
  "port": 8756,
  "token": "$token",
  "saveDir": "C:\\ClinicArchive\\clinic_data\\staging",
  "archiveMode": "by_patient",
  "selectedIp": null,
  "connectionMode": "lan",
  "maxUploadMb": 20,
  "maxUploadCount": 20
}
"@ | Set-Content -Encoding ASCII $snapCfg
# 注意：必須用 ASCII/無 BOM 寫入。PowerShell 5 的 -Encoding UTF8 會帶 BOM，
# ClinicSnap 會把 BOM JSON 當損毀檔而改用預設值重建，saveDir 接線就靜默失效。
```

防火牆。**先確認這台機器的網路被歸類成什麼**——`profile=private,domain` 的規則在被歸類為
Public 的網路上完全不生效，手機會連不到，而規則本身卻「新增成功」，很容易誤判：

```powershell
Get-NetConnectionProfile | Select-Object Name, InterfaceAlias, NetworkCategory
```

- `NetworkCategory` 是 `Private` 或 `DomainAuthenticated` → 往下做。
- 是 `Public` → **停下來告訴人類**：要嘛請他到「設定 → 網路和網際網路 → 該連線 → 網路設定檔」
  改成「私人」，要嘛由你執行下面這行（**需要 UAC，執行前先講清楚你要改什麼、為什麼**）：
  ```powershell
  Set-NetConnectionProfile -InterfaceAlias "<上面查到的 InterfaceAlias>" -NetworkCategory Private
  ```
  改完重跑 `Get-NetConnectionProfile` 確認，再繼續。**不要**改用 `profile=any` 繞過去——那等於
  把 8756/8770 開給公共網路，違反 architecture §8。

確認是 Private/Domain 之後再加規則（需 UAC，先告知人類這兩條規則的用途：手機連 ClinicSnap:8756、
同仁瀏覽器連整合層:8770，僅限私人/網域網路）：

```powershell
netsh advfirewall firewall add rule name="ClinicSnap 8756" dir=in action=allow protocol=TCP localport=8756 profile=private,domain
netsh advfirewall firewall add rule name="ClinicArchive Web 8770" dir=in action=allow protocol=TCP localport=8770 profile=private,domain
```

**驗證**：啟動 `ClinicSnap.exe`，視窗顯示 QR 與「儲存資料夾 = C:\ClinicArchive\clinic_data\staging」「依病患」。若它跳防火牆詢問，請人類按允許。再回讀設定確認未被重建：

```powershell
if (-not $exeDir) { throw '$exeDir 未設定：驗證無效，請先重新偵測 ClinicSnap.exe 位置。' }
Select-String -Path (Join-Path $exeDir "config.json") -Pattern "staging"   # 必須有命中；沒有＝config 被重建，回頭檢查編碼
Test-Path (Join-Path $exeDir "config.json.bak")                            # 應為 False；True＝寫入格式曾被判損毀
```

## Step 5｜常駐與開機自啟

### 5a｜先請人類加 Defender 排除（首次啟動之前做）

Windows Defender 的即時掃描會在照片剛落地時**短暫鎖住檔案**。整合層的 watcher 讀不到就重試，
在忙碌時段可能變成無限重試、log 洗版、批次卡住不歸檔。這是主動步驟，不是等出事再處理：

**告訴人類**：「請把 `C:\ClinicArchive` 與 `C:\ClinicSnap` 兩個資料夾加入 Windows 安全性的
掃描排除項目（設定 → 隱私權與安全性 → Windows 安全性 → 病毒與威脅防護 → 管理設定 →
排除項目 → 新增排除項目 → 資料夾）。這需要系統管理員權限（UAC），而且只排除這兩個我們自己
的資料夾，不會關掉整台機器的防毒。」

排除做完再往下。（同時也解決防毒把 `ClinicSnap.exe` 隔離的問題。）

### 5b｜預寫整合層 config.json（**首次啟動之前**，這步不能跳）

整合層的 `config.json` 與 `data_root` 預設都是**相對路徑**，落點由「啟動當下的工作目錄」決定。
只要有人哪一次從別的目錄啟動，就會生出**第二套** `clinic_data\`，資料悄悄分裂成兩份。
在第一次啟動前就把 `data_root` 釘成絕對路徑，這個坑就永遠不會發生：

```powershell
# 用 @'...'@（單引號 here-string，完全不做變數展開）：這段裡沒有任何要展開的東西，
# 而 {data_root} 是給整合層自己解讀的佔位符，必須原樣寫進檔案。
@'
{
  "data_root": "C:\\ClinicArchive\\clinic_data",
  "db_path": "{data_root}/clinic.db",
  "web_host": "0.0.0.0",
  "web_port": 8770,
  "settle_seconds": 10,
  "poll_seconds": 3,
  "ocr_version": "PPOCRV6",
  "det_side_len": 960,
  "session_hours": 12,
  "allowed_exts": [".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"]
}
'@ | Set-Content -Encoding ASCII C:\ClinicArchive\config.json
```

同樣**必須 ASCII／無 BOM**：整合層以 UTF-8 讀取，BOM 會讓 JSON 解析失敗，
它會把檔案備份成 `config.json.bak` 後**用預設值重建**，你設的絕對路徑就沒了。
其餘欄位就是 `integration/clinic_archive/config.py` 的預設值（欄位名以該檔為準）；
`{data_root}` 是佔位符，載入時會展開成上面那個絕對路徑，照原樣寫進去即可。

**驗證**：
```powershell
Test-Path C:\ClinicArchive\config.json.bak    # 應為 False；True＝寫入編碼有問題，重寫一次
```

### 5c｜啟動腳本與開機自啟捷徑

```powershell
@'
@echo off
cd /d C:\ClinicArchive
repo\integration\.venv\Scripts\python.exe repo\integration\run.py --config C:\ClinicArchive\config.json
'@ | Set-Content -Encoding ASCII C:\ClinicArchive\start_integration.bat

if (-not $exeDir) { throw '$exeDir 未設定：請回 Step 4 重新偵測 ClinicSnap.exe 位置，否則捷徑會指向錯誤路徑。' }
$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$s1 = $ws.CreateShortcut("$startup\ClinicSnap.lnk");        $s1.TargetPath = (Join-Path $exeDir "ClinicSnap.exe"); $s1.WorkingDirectory = $exeDir; $s1.Save()
$s2 = $ws.CreateShortcut("$startup\ClinicArchive.lnk");     $s2.TargetPath = "C:\ClinicArchive\start_integration.bat"; $s2.Save()
```

`--config` 用絕對路徑指定，是為了讓「從哪個目錄啟動」再也影響不到設定檔落點——與 5b 的絕對
`data_root` 是雙保險。

啟動整合層（第一次手動跑，之後靠開機自啟）：執行 `C:\ClinicArchive\start_integration.bat`（會留一個主控台視窗，屬正常、方便看 log）。

**驗證**：
```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8770/login).StatusCode   # 200
Test-Path C:\ClinicArchive\clinic_data\clinic.db                              # True＝整合層真的起來過
Test-Path C:\ClinicArchive\clinic_data\FIRST_RUN_ADMIN.txt                    # True（首次）
```

> 用 `127.0.0.1` 而不是 `localhost`：`localhost` 在 Windows 常先解析到 IPv6 的 `::1`，
> 而服務綁的是 IPv4，於是連不上——是解析問題，不是服務沒起來，卻很容易被誤判成失敗。

然後**告訴人類**：「admin 首次密碼在 `C:\ClinicArchive\clinic_data\FIRST_RUN_ADMIN.txt`，請你自己開瀏覽器 `http://127.0.0.1:8770` 登入 → 右上角「改密碼」換掉 admin 密碼（至少 8 字元；改完其他裝置的 admin 登入會全部失效）→ 建立每位同仁帳號（管理者/一般）→ **刪除該密碼檔**」。不要替他讀出內容。日後同仁忘記密碼：管理員在「帳號」頁重設；admin 本人忘記：在伺服器主控台 `cd C:\ClinicArchive\repo\integration` 後執行 `.\.venv\Scripts\python run.py --config C:\ClinicArchive\config.json --set-password admin`（提示輸入、不回顯；這一步由人類自己敲，agent 不代打）。

> **⚠️ `FIRST_RUN_ADMIN.txt` 在 Windows 上沒有權限保護。**
> 程式會盡力收緊權限，但那是 best-effort：只要這台機器上有其他帳號、或資料夾被分享／被備份到
> NAS，這個**明碼密碼檔**就可能被別人看到。所以它不是「有空再刪」，是**登入改密之後立刻刪**。
> 請人類當場刪掉，並在完成報告裡記錄他已經刪了。

請人類設定這台機器「開機自動登入」（ClinicSnap 是桌面程式，需要登入的工作階段）。

## Step 6｜驗收（全綠才算部署完成）

跑 `deploy\verify.ps1`（本資料夾）並逐項核對；或手動：

> 表中用到 `$exeDir` 的那格，一樣必須在 Step 4 的偵測指令跑過的**同一個視窗**執行；換了視窗先重跑那兩行。
> 連通性一律測 `127.0.0.1` 不測 `localhost`（`localhost` 會解析到 `::1` 而誤報失敗）。

### 6a｜隱私前提閘門（**先做，過了才准用真資料**）

驗收表的 3–5 會用到真的健保卡與真的檢驗報告；在下列控制**確認到位之前**，任何真實身份資料都不得經過這套系統（手機 → 伺服器目前是明文 HTTP，architecture §8）。請人類逐項回答，你只記錄、不代答：

- [ ] 員工 SSID 已用 WPA3-SAE；AP client isolation 已開並**實測**（手機→伺服器通、手機→手機不通）；路由器 ACL 限員工 SSID 只達伺服器
- [ ] BitLocker 已開、金鑰已收妥
- [ ] `FIRST_RUN_ADMIN.txt` 已改密並刪除（Step 5c）

網路三控制做不到時，改做 `deploy\TLS_EARLY.md`（TLS 反向代理提前方案），其 §8 驗證清單全過即可視同第一項已勾。三項全勾 → 做完整驗收表（1–6）。**任一項未勾** → 只做 1、2、6，**3–5 改用合成資料**（`.\.venv\Scripts\python contract_test.py --simulate` 產生的樣本，或 `pytest tests\test_pipeline_e2e.py`），完成報告標「**部分驗收：待網路控制到位後補真卡／真報告測試**」並回報 Albert 啟動 TLS 提前方案。不要因為「只是測一下」就先拍真卡。

### 6b｜驗收表

| # | 測什麼 | 指令/動作 | 過關 |
|---|---|---|---|
| 1 | 兩服務在聽 | `Test-NetConnection 127.0.0.1 -Port 8756`、`Test-NetConnection 127.0.0.1 -Port 8770` | 皆 TcpTestSucceeded True |
| 2 | 契約（真實輸出，**不含身份資料**） | 請人類用手機掃 QR，**不輸入任何病患代碼、不拍卡**，對白紙或桌面拍 2 張送出後：`.\.venv\Scripts\python contract_test.py C:\ClinicArchive\clinic_data\staging --config (Join-Path $exeDir "config.json")` | `CONTRACT: PASS`、無 archiveMode 警告（輸出已遮罩，可貼） |
| 3 | 端到端首批（**需 6a 全勾**） | 人類照 N0 協定（先拍卡再拍患部）用自己的健保卡實拍一批 | 首見證號**進佇列**（設計如此）→ 人類在網頁確認建檔 → `archive\` 出現資料夾 |
| 4 | 自動歸檔（**需 6a 全勾**） | 同一張卡再拍第二批 | 這批自動歸檔、時間軸可見 |
| 5 | 報告流（**需 6a 全勾**） | 人類從 LINE 拖一張報告圖進 `clinic_data\inbox\` | 進佇列或自動歸檔，佇列頁欄位正確 |
| 6 | 重開機演練 | 重開機 → 自動登入 → 兩服務自動起來 | 步驟 1 重測通過 |

## Step 7｜輸出「人類實體清單」並收工

部署報告最後附這張表（agent 驗不了的實體項，請人類勾）：

- [ ] 伺服器固定 IP（DHCP 保留）已設
- [ ] 員工 SSID 已用 WPA3-SAE；AP client isolation 已開並**實測**（手機→伺服器通、手機→手機不通）；路由器 ACL 限員工 SSID 只達伺服器──**任一項做不到，先不要放真病人資料，改做 `deploy\TLS_EARLY.md` 的 TLS 提前方案並回報 Albert**（architecture §8）
- [ ] BitLocker 已開、金鑰已收妥
- [ ] QR 已印、只貼員工區
- [ ] 同意書臨床攝影條款、員工守則增補已生效
- [ ] NAS 每日備份已排（可後補，但要有時間表）

## 完成報告格式

回報人類：各步驟結果表（含 Step 3 測試數字原文）、兩服務版本與路徑、驗收表、人類清單、以及任何你判斷不了而擱置的事項。誠實優先：沒過的就寫沒過。

## 疑難排解速查

| 症狀 | 先看 |
|---|---|
| 手機掃 QR 打不開 | 手機是否在員工 Wi-Fi；`Get-NetConnectionProfile` 的 NetworkCategory 是否為 Public（是的話 `profile=private,domain` 的防火牆規則不生效，見 Step 4）；AP 隔離是否連「無線→有線」也擋（部分低階 AP 會，屬 architecture §8 的控制不成立情境） |
| 8770 起不來 | 主控台視窗錯誤訊息；port 被佔 `netstat -ano \| findstr 8770` |
| **出現第二套 `clinic_data\`**（或 8770 起來了但看不到先前的資料） | 從**錯誤的工作目錄**啟動過整合層。確認 Step 5b 的 `C:\ClinicArchive\config.json` 存在、`data_root` 是絕對路徑，且 `start_integration.bat` 帶了 `--config C:\ClinicArchive\config.json`。多出來的那套 `clinic_data\` 先**不要刪**，回報 Albert 決定怎麼合併 |
| `import onnxruntime` 失敗、`DLL load failed while importing onnxruntime_pybind11_state` | 缺 VC++ 執行階段。請人類裝 **Microsoft Visual C++ 2015-2022 Redistributable (x64)**（`winget install -e --id Microsoft.VCRedist.2015+.x64`，需 UAC），裝完開新視窗重跑 Step 2 的驗證 |
| `python` 沒反應／跳 Microsoft Store／venv 建出來是壞的 | App Execution Alias 存根（見 Step 1 警告）。關掉 `python.exe`／`python3.exe` 別名，改用 `py -3.12`，並把 `.venv` 整個刪掉重建 |
| watcher 一直重試同一個檔、log 洗版、照片卡著不歸檔 | Defender 即時掃描鎖檔。確認 Step 5a 的 `C:\ClinicArchive`、`C:\ClinicSnap` 排除項目真的加上去了 |
| OCR 模型下載失敗 | 網路／proxy；重跑 Step 3 的 e2e |
| `Invoke-WebRequest` 下載 ClinicSnap 失敗／連線被關閉 | TLS：先跑 `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12` 再重試（見 Step 4） |
| 防毒隔離 exe | 把 `C:\ClinicSnap`、`C:\ClinicArchive` 加白名單（請人類操作，見 Step 5a） |
| ClinicSnap 視窗打不開 | 裝 Microsoft Edge WebView2 Runtime（Win11 內建；Win10 需裝） |
| ClinicSnap 的 saveDir 又變回 `我的圖片\ClinicSnap` | config 被判損毀重建。檢查 `config.json.bak` 是否出現、寫入時是否誤用了帶 BOM 的 UTF8；並確認當時 `$exeDir` 不是空的（見 Step 4 警告） |
