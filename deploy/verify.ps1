# verify.ps1 — 部署驗收電池（AGENT_DEPLOY.md Step 6 的機器可驗部分）
# 用法：powershell -ExecutionPolicy Bypass -File verify.ps1
# 結尾輸出 VERIFY: PASS / FAIL（人類實體項不在本腳本範圍，見 AGENT_DEPLOY Step 7）。
#
# 相容性：PowerShell 5.1。全檔不得使用 PS7 專屬語法（&&、||、??、三元 ? :、
# $PSNativeCommandUseErrorActionPreference）。
#
# 連通性一律測 127.0.0.1 不測 localhost：localhost 在 Windows 常先解析到 IPv6 的 ::1，
# 而服務綁在 IPv4，會連不上而誤報成「服務沒起來」。

$ErrorActionPreference = "Continue"
$fail = 0
function Check($name, $ok, $detail) {
  if ($ok) { Write-Host ("PASS  {0}" -f $name) }
  else { Write-Host ("FAIL  {0}  {1}" -f $name, $detail); $script:fail++ }
}

$py = "C:\ClinicArchive\repo\integration\.venv\Scripts\python.exe"
$contract = "C:\ClinicArchive\repo\integration\contract_test.py"
$dataRoot = "C:\ClinicArchive\clinic_data"
$staging = Join-Path $dataRoot "staging"

# 1) 目錄與 venv
Check "repo 存在" (Test-Path "C:\ClinicArchive\repo\integration\run.py") "缺 C:\ClinicArchive\repo"
Check "venv python 存在" (Test-Path $py) "缺 .venv（先跑 AGENT_DEPLOY Step 2）"

# clinic_data 是否「被整合層初始化過」——注意不能拿 staging\ 當證據：
# ClinicSnap 自己就會把 saveDir 建出來，staging 存在只代表 ClinicSnap 跑過，
# 不代表整合層啟動過。clinic.db／integration.log 才是整合層留下的痕跡。
$dbPath = Join-Path $dataRoot "clinic.db"
$logPath = Join-Path $dataRoot "integration.log"
Check "clinic_data 已初始化" ((Test-Path $dbPath) -or (Test-Path $logPath)) `
  "找不到 clinic.db 或 integration.log＝整合層尚未首次啟動（staging\ 存在不算數，那是 ClinicSnap 建的）"

# 2) 兩服務在聽
$t1 = Test-NetConnection -ComputerName 127.0.0.1 -Port 8756 -WarningAction SilentlyContinue
Check "ClinicSnap :8756" $t1.TcpTestSucceeded "ClinicSnap 未啟動或防火牆未放行"
$t2 = Test-NetConnection -ComputerName 127.0.0.1 -Port 8770 -WarningAction SilentlyContinue
Check "整合層 :8770" $t2.TcpTestSucceeded "start_integration.bat 未啟動"

# 3) 整合層 /login 200
try {
  $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 http://127.0.0.1:8770/login
  Check "/login 回 200" ($r.StatusCode -eq 200) ("HTTP " + $r.StatusCode)
} catch { Check "/login 回 200" $false $_.Exception.Message }

# 4) ClinicSnap config 接線
# 由 exe 位置推導 config.json（ClinicSnap 的設定檔固定與 exe 同層），不要用 -Filter config.json
# 全碟遞迴撈——那可能撈到別的東西（例如 .bak 旁邊的殘檔或其他程式的同名檔）而驗錯對象。
$snapExe = Get-ChildItem C:\ClinicSnap -Recurse -Filter ClinicSnap.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$cfgPath = $null
if ($snapExe) { $cfgPath = Join-Path $snapExe.DirectoryName "config.json" }

if ($cfgPath -and (Test-Path $cfgPath)) {
  $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
  Check "archiveMode=by_patient" ($cfg.archiveMode -eq "by_patient") ("目前=" + $cfg.archiveMode)
  Check "saveDir 指向 staging" ($cfg.saveDir -ieq $staging) ("目前=" + $cfg.saveDir)
  Check "connectionMode=lan" ($cfg.connectionMode -eq "lan") ("目前=" + $cfg.connectionMode + "（院內政策禁 tunnel）")
  # config.json.bak 出現＝我們寫入的格式曾被判損毀、設定被重建（多半是 BOM）
  Check "ClinicSnap config 未被重建" (-not (Test-Path ($cfgPath + ".bak"))) `
    "出現 config.json.bak＝寫入格式曾被判損毀，saveDir 接線可能已失效（見 AGENT_DEPLOY Step 4）"
} elseif ($snapExe) {
  Check "ClinicSnap config.json" $false ("exe 在 " + $snapExe.DirectoryName + " 但同層沒有 config.json（未首跑或未預寫）")
} else {
  Check "ClinicSnap config.json" $false "在 C:\ClinicSnap 找不到 ClinicSnap.exe（未安裝或被防毒隔離）"
}

# 4b) 整合層 config：data_root 必須是絕對路徑（否則換個目錄啟動就生出第二套 clinic_data）
$intCfgPath = "C:\ClinicArchive\config.json"
if (Test-Path $intCfgPath) {
  $intCfg = Get-Content $intCfgPath -Raw | ConvertFrom-Json
  Check "整合層 data_root 為絕對路徑" ([System.IO.Path]::IsPathRooted([string]$intCfg.data_root)) `
    ("目前=" + $intCfg.data_root + "（相對路徑會隨啟動目錄漂移，見 AGENT_DEPLOY Step 5b）")
  Check "整合層 config 未被重建" (-not (Test-Path ($intCfgPath + ".bak"))) `
    "出現 config.json.bak＝寫入編碼有問題（BOM），設定已被重建成預設值"
} else {
  Check "整合層 config.json" $false "缺 C:\ClinicArchive\config.json（見 AGENT_DEPLOY Step 5b，必須在首次啟動前預寫）"
}

# 5) 契約自測（不動真資料）
if (Test-Path $py) {
  & $py $contract --selftest *> $env:TEMP\contract_selftest.txt
  $last = (Get-Content $env:TEMP\contract_selftest.txt | Select-Object -Last 1)
  Check "contract --selftest" (("$last".Trim()) -eq "CONTRACT: PASS") $last
  # 6) 真實 staging 契約（staging 有檔才有意義；空的跳過不算失敗）
  $hasFiles = (Get-ChildItem $staging -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0
  if ($hasFiles -and $cfgPath -and (Test-Path $cfgPath)) {
    & $py $contract $staging --config $cfgPath *> $env:TEMP\contract_real.txt
    $last2 = (Get-Content $env:TEMP\contract_real.txt | Select-Object -Last 1)
    Check "contract（真實 staging）" (("$last2".Trim()) -eq "CONTRACT: PASS") $last2
  } else { Write-Host "SKIP  contract（真實 staging）：staging 目前無檔（拍一批測試照後重跑）" }
}

# 7) 開機自啟捷徑
$startup = [Environment]::GetFolderPath('Startup')
Check "自啟：ClinicSnap.lnk" (Test-Path "$startup\ClinicSnap.lnk") "缺捷徑（AGENT_DEPLOY Step 5）"
Check "自啟：ClinicArchive.lnk" (Test-Path "$startup\ClinicArchive.lnk") "缺捷徑（AGENT_DEPLOY Step 5）"

Write-Host ""
if ($fail -eq 0) { Write-Host "VERIFY: PASS" } else { Write-Host ("VERIFY: FAIL ({0} 項)" -f $fail) }
