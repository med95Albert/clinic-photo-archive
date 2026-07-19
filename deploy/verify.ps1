# verify.ps1 — 部署驗收電池（AGENT_DEPLOY.md Step 6 的機器可驗部分）
# 用法：powershell -ExecutionPolicy Bypass -File verify.ps1
# 結尾輸出 VERIFY: PASS / FAIL（人類實體項不在本腳本範圍，見 AGENT_DEPLOY Step 7）。

$ErrorActionPreference = "Continue"
$fail = 0
function Check($name, $ok, $detail) {
  if ($ok) { Write-Host ("PASS  {0}" -f $name) }
  else { Write-Host ("FAIL  {0}  {1}" -f $name, $detail); $script:fail++ }
}

$py = "C:\ClinicArchive\repo\integration\.venv\Scripts\python.exe"
$contract = "C:\ClinicArchive\repo\integration\contract_test.py"
$staging = "C:\ClinicArchive\clinic_data\staging"

# 1) 目錄與 venv
Check "repo 存在" (Test-Path "C:\ClinicArchive\repo\integration\run.py") "缺 C:\ClinicArchive\repo"
Check "venv python 存在" (Test-Path $py) "缺 .venv（先跑 AGENT_DEPLOY Step 2）"
Check "clinic_data 已初始化" (Test-Path $staging) "整合層尚未首次啟動"

# 2) 兩服務在聽
$t1 = Test-NetConnection -ComputerName localhost -Port 8756 -WarningAction SilentlyContinue
Check "ClinicSnap :8756" $t1.TcpTestSucceeded "ClinicSnap 未啟動或防火牆未放行"
$t2 = Test-NetConnection -ComputerName localhost -Port 8770 -WarningAction SilentlyContinue
Check "整合層 :8770" $t2.TcpTestSucceeded "start_integration.bat 未啟動"

# 3) 整合層 /login 200
try {
  $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 http://localhost:8770/login
  Check "/login 回 200" ($r.StatusCode -eq 200) ("HTTP " + $r.StatusCode)
} catch { Check "/login 回 200" $false $_.Exception.Message }

# 4) ClinicSnap config 接線
$cfgPath = Get-ChildItem C:\ClinicSnap -Recurse -Filter config.json -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cfgPath) {
  $cfg = Get-Content $cfgPath.FullName -Raw | ConvertFrom-Json
  Check "archiveMode=by_patient" ($cfg.archiveMode -eq "by_patient") ("目前=" + $cfg.archiveMode)
  Check "saveDir 指向 staging" ($cfg.saveDir -ieq $staging) ("目前=" + $cfg.saveDir)
  Check "connectionMode=lan" ($cfg.connectionMode -eq "lan") ("目前=" + $cfg.connectionMode + "（院內政策禁 tunnel）")
} else { Check "ClinicSnap config.json" $false "找不到（ClinicSnap 未安裝或未首跑）" }

# 5) 契約自測（不動真資料）
if (Test-Path $py) {
  & $py $contract --selftest *> $env:TEMP\contract_selftest.txt
  $last = (Get-Content $env:TEMP\contract_selftest.txt | Select-Object -Last 1)
  Check "contract --selftest" ($last -eq "CONTRACT: PASS") $last
  # 6) 真實 staging 契約（staging 有檔才有意義；空的跳過不算失敗）
  $hasFiles = (Get-ChildItem $staging -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0
  if ($hasFiles -and $cfgPath) {
    & $py $contract $staging --config $cfgPath.FullName *> $env:TEMP\contract_real.txt
    $last2 = (Get-Content $env:TEMP\contract_real.txt | Select-Object -Last 1)
    Check "contract（真實 staging）" ($last2 -eq "CONTRACT: PASS") $last2
  } else { Write-Host "SKIP  contract（真實 staging）：staging 目前無檔（拍一批測試照後重跑）" }
}

# 7) 開機自啟捷徑
$startup = [Environment]::GetFolderPath('Startup')
Check "自啟：ClinicSnap.lnk" (Test-Path "$startup\ClinicSnap.lnk") "缺捷徑（AGENT_DEPLOY Step 5）"
Check "自啟：ClinicArchive.lnk" (Test-Path "$startup\ClinicArchive.lnk") "缺捷徑（AGENT_DEPLOY Step 5）"

Write-Host ""
if ($fail -eq 0) { Write-Host "VERIFY: PASS" } else { Write-Host ("VERIFY: FAIL ({0} 項)" -f $fail) }
