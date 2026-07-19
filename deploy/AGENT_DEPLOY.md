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
3. **絕不讀取病人資料入對話**：`clinic_data\` 底下的 `archive\`、`review\`、`staging\`、`inbox\`、`trash\` 內容一律不 cat/type/開檔；`clinic.db` 不 SELECT 病人列。日誌檔（`integration.log`、`clinic_snap.log`）可以讀。
4. **密碼不落對話**：`FIRST_RUN_ADMIN.txt` 的內容不得印出——只告訴人類檔案路徑，請他自己開。
5. **不越界**：不碰 HIS、不裝清單外軟體、不改系統安全設定（防火牆規則除外，且要人類看到並同意）、不啟用 ClinicSnap 的網際網路（tunnel）模式。
6. 需要系統管理員權限的步驟（防火牆、winget 裝軟體），先明講再請人類允許 UAC。
7. 全程用 PowerShell；路徑一律絕對路徑。

## 固定路徑（全文件通用）

```
C:\ClinicArchive\                 ← 一切的家（工作目錄）
C:\ClinicArchive\repo\            ← 本 repo clone 於此
C:\ClinicArchive\clinic_data\     ← 整合層資料（首次啟動自動建）
C:\ClinicSnap\                    ← ClinicSnap 解壓於此
```

## Step 1｜前置檢查

```powershell
[System.Environment]::OSVersion.Version          # Windows 10+；主版本 >= 10
(Get-PSDrive C).Free/1GB                          # 建議 >= 200GB，理想 1TB
python --version                                  # 需 3.11+；沒有 → winget install -e --id Python.Python.3.12（需 UAC，先告知人類）
git --version                                     # 沒有 → winget install -e --id Git.Git；或改用「下載 repo zip」路線
ping -n 1 github.com                              # 對外網路（下載依賴與模型要用）
```

裝完 Python/Git 後**開新的 PowerShell 視窗**再繼續（PATH 才會生效）。
另外問人類一句：「這台機器的固定 IP（DHCP 保留）設好了嗎？」——沒設不擋安裝，但要記進最後的人類清單。

## Step 2｜取得 repo 與整合層環境

```powershell
New-Item -ItemType Directory -Force C:\ClinicArchive | Out-Null
cd C:\ClinicArchive
git clone https://github.com/med95Albert/clinic-photo-archive.git repo   # 已存在則 git -C repo pull
cd repo\integration
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
```

**驗證**：`.\.venv\Scripts\python -c "import fastapi, rapidocr, onnxruntime, PIL; print('deps OK')"` 印出 `deps OK`。

## Step 3｜整合層測試（在這台機器上跑一次）

```powershell
cd C:\ClinicArchive\repo\integration
.\.venv\Scripts\python -m pytest tests -q          # 期望全綠
.\.venv\Scripts\python -m pytest tests\test_ocr_live.py -m e2e -q   # 首次會下載 OCR 模型（數十 MB），必須 2 passed
.\.venv\Scripts\python contract_test.py --selftest  # 結尾必須 CONTRACT: PASS
```

**判斷規則**：`test_ocr_live` 與 `--selftest` 是硬閘門，不過就停。一般套件若出現個位數失敗且內容是「檔案權限位元、路徑大小寫」這類 Windows 環境差異，**不要改程式碼**——原文記下、繼續部署、寫進完成報告給 Albert 裁決；若失敗發生在 `test_predicate`／`test_batching`／`test_archiver`／`test_provenance`（核心正確性），一律停下回報。

## Step 4｜安裝 ClinicSnap（上游官方 release）

```powershell
$zip = "$env:TEMP\ClinicSnap.zip"
Invoke-WebRequest -Uri "https://github.com/leon80148/ClinicSnap/releases/download/v0.1.2/ClinicSnap-0.1.2-win64.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath C:\ClinicSnap -Force
Get-ChildItem C:\ClinicSnap -Recurse -Filter ClinicSnap.exe | Select-Object -First 1   # 確認 exe 位置（zip 可能多包一層資料夾，下述 $exeDir 以實際為準）
```

預寫 ClinicSnap 設定（**接線關鍵**：saveDir 指向整合層的 staging；歸檔模式 by_patient；區網模式）。
把 `$exeDir` 換成上一步找到的 `ClinicSnap.exe` 所在資料夾：

```powershell
$token = & C:\ClinicArchive\repo\integration\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(16))"
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
"@ | Set-Content -Encoding ASCII "$exeDir\config.json"
# 注意：必須用 ASCII/無 BOM 寫入。PowerShell 5 的 -Encoding UTF8 會帶 BOM，
# ClinicSnap 會把 BOM JSON 當損毀檔而改用預設值重建，saveDir 接線就靜默失效。
```

防火牆（需 UAC，先告知人類這兩條規則的用途：手機連 ClinicSnap:8756、同仁瀏覽器連整合層:8770，僅限私人/網域網路）：

```powershell
netsh advfirewall firewall add rule name="ClinicSnap 8756" dir=in action=allow protocol=TCP localport=8756 profile=private,domain
netsh advfirewall firewall add rule name="ClinicArchive Web 8770" dir=in action=allow protocol=TCP localport=8770 profile=private,domain
```

**驗證**：啟動 `ClinicSnap.exe`，視窗顯示 QR 與「儲存資料夾 = C:\ClinicArchive\clinic_data\staging」「依病患」。若它跳防火牆詢問，請人類按允許。再回讀設定確認未被重建：

```powershell
Select-String -Path "$exeDir\config.json" -Pattern "staging"   # 必須有命中；沒有＝config 被重建，回頭檢查編碼
Test-Path "$exeDir\config.json.bak"                            # 應為 False；True＝寫入格式曾被判損毀
```

## Step 5｜常駐與開機自啟

```powershell
@'
@echo off
cd /d C:\ClinicArchive
repo\integration\.venv\Scripts\python.exe repo\integration\run.py
'@ | Set-Content -Encoding ASCII C:\ClinicArchive\start_integration.bat

$startup = [Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$s1 = $ws.CreateShortcut("$startup\ClinicSnap.lnk");        $s1.TargetPath = "$exeDir\ClinicSnap.exe"; $s1.WorkingDirectory = $exeDir; $s1.Save()
$s2 = $ws.CreateShortcut("$startup\ClinicArchive.lnk");     $s2.TargetPath = "C:\ClinicArchive\start_integration.bat"; $s2.Save()
```

啟動整合層（第一次手動跑，之後靠開機自啟）：執行 `C:\ClinicArchive\start_integration.bat`（會留一個主控台視窗，屬正常、方便看 log）。

**驗證**：
```powershell
(Invoke-WebRequest -UseBasicParsing http://localhost:8770/login).StatusCode   # 200
Test-Path C:\ClinicArchive\clinic_data\FIRST_RUN_ADMIN.txt                    # True（首次）
```
然後**告訴人類**：「admin 首次密碼在 `C:\ClinicArchive\clinic_data\FIRST_RUN_ADMIN.txt`，請你自己開瀏覽器 `http://localhost:8770` 登入 → 建立每位同仁帳號（管理者/一般）→ 刪除該密碼檔」。不要替他讀出內容。
請人類設定這台機器「開機自動登入」（ClinicSnap 是桌面程式，需要登入的工作階段）。

## Step 6｜驗收（全綠才算部署完成）

跑 `deploy\verify.ps1`（本資料夾）並逐項核對；或手動：

| # | 測什麼 | 指令/動作 | 過關 |
|---|---|---|---|
| 1 | 兩服務在聽 | `Test-NetConnection localhost -Port 8756`、`-Port 8770` | 皆 TcpTestSucceeded True |
| 2 | 契約（真實輸出） | 請人類用手機掃 QR 拍 2 張測試照送出後：`.\.venv\Scripts\python contract_test.py C:\ClinicArchive\clinic_data\staging --config "$exeDir\config.json"` | `CONTRACT: PASS`、無 archiveMode 警告 |
| 3 | 端到端首批 | 人類照 N0 協定（先拍卡再拍患部）用自己的健保卡實拍一批 | 首見證號**進佇列**（設計如此）→ 人類在網頁確認建檔 → `archive\` 出現資料夾 |
| 4 | 自動歸檔 | 同一張卡再拍第二批 | 這批自動歸檔、時間軸可見 |
| 5 | 報告流 | 人類從 LINE 拖一張報告圖進 `clinic_data\inbox\` | 進佇列或自動歸檔，佇列頁欄位正確 |
| 6 | 重開機演練 | 重開機 → 自動登入 → 兩服務自動起來 | 步驟 1 重測通過 |

## Step 7｜輸出「人類實體清單」並收工

部署報告最後附這張表（agent 驗不了的實體項，請人類勾）：

- [ ] 伺服器固定 IP（DHCP 保留）已設
- [ ] 員工 SSID 已用 WPA3-SAE；AP client isolation 已開並**實測**（手機→伺服器通、手機→手機不通）；路由器 ACL 限員工 SSID 只達伺服器──**任一項做不到，先不要放真病人資料，回報 Albert 啟動 TLS 提前方案**（architecture §8）
- [ ] BitLocker 已開、金鑰已收妥
- [ ] QR 已印、只貼員工區
- [ ] 同意書臨床攝影條款、員工守則增補已生效
- [ ] NAS 每日備份已排（可後補，但要有時間表）

## 完成報告格式

回報人類：各步驟結果表（含 Step 3 測試數字原文）、兩服務版本與路徑、驗收表、人類清單、以及任何你判斷不了而擱置的事項。誠實優先：沒過的就寫沒過。

## 疑難排解速查

| 症狀 | 先看 |
|---|---|
| 手機掃 QR 打不開 | 手機是否在員工 Wi-Fi；AP 隔離是否連「無線→有線」也擋（部分低階 AP 會，屬 architecture §8 的控制不成立情境） |
| 8770 起不來 | 主控台視窗錯誤訊息；port 被佔 `netstat -ano \| findstr 8770` |
| OCR 模型下載失敗 | 網路／proxy；重跑 Step 3 的 e2e |
| 防毒隔離 exe | 把 `C:\ClinicSnap`、`C:\ClinicArchive` 加白名單（請人類操作） |
| ClinicSnap 視窗打不開 | 裝 Microsoft Edge WebView2 Runtime（Win11 內建；Win10 需裝） |
