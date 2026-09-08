# bootstrap.ps1 — 環境建置加速器（冪等；AGENT_DEPLOY.md Step 2 的腳本版）
# 假設 Python 3.12（含 py launcher）與 Git 已在 PATH（沒有 → 先照 AGENT_DEPLOY Step 1 用 winget 安裝）。
# 本腳本刻意只做「建目錄＋clone/更新＋venv＋鎖版依賴」，其餘步驟由 agent 照 runbook 逐步驗證執行。
#
# 相容性：PowerShell 5.1（診所機預設版本）。全檔不得使用 PS7 專屬語法
# （&&、||、??、三元 ? :、$PSNativeCommandUseErrorActionPreference）。
#
# 為什麼不用 $ErrorActionPreference = "Stop"：
#   它只管 PowerShell cmdlet 的 terminating error，對 python/git/pip 這類「原生 exe」完全無效
#   ——原生指令失敗只會設 $LASTEXITCODE，腳本照樣往下跑。
#   更糟的是在某些版本它會把 git 寫到 stderr 的**正常進度訊息**當成錯誤而誤停。
#   所以改成 Continue，並在每個原生指令後自己檢查 $LASTEXITCODE。

$ErrorActionPreference = "Continue"

function Assert-ExitCode($step) {
  if ($LASTEXITCODE -ne 0) { throw ("{0} 失敗（exit {1}）" -f $step, $LASTEXITCODE) }
}

# 一律用 py launcher 指定版本：Windows 的 python.exe / python3.exe 可能是
# 「應用程式執行別名」存根，會跳 Microsoft Store 或建出壞掉的 venv。
py -3.12 --version
Assert-ExitCode "py -3.12 --version（Python 3.12 未安裝，或 App Execution Alias 存根擋在前面）"

git --version
Assert-ExitCode "git --version"

New-Item -ItemType Directory -Force C:\ClinicArchive | Out-Null
Set-Location C:\ClinicArchive

if (Test-Path .\repo\.git) {
  git -C repo pull --ff-only
  Assert-ExitCode "git pull --ff-only"
} else {
  git clone https://github.com/med95Albert/clinic-photo-archive.git repo
  Assert-ExitCode "git clone"
}

Set-Location C:\ClinicArchive\repo\integration

if (-not (Test-Path .\requirements.lock)) {
  throw "找不到 requirements.lock：這是唯一的依賴真相，缺了就停下回報人類，不要退回未鎖版安裝。"
}

if (-not (Test-Path .venv)) {
  py -3.12 -m venv .venv
  Assert-ExitCode "py -3.12 -m venv .venv"
}

.\.venv\Scripts\pip install -r requirements.lock
Assert-ExitCode "pip install -r requirements.lock"

.\.venv\Scripts\pip install -e . --no-deps
Assert-ExitCode "pip install -e . --no-deps"

.\.venv\Scripts\python -c "import fastapi, rapidocr, onnxruntime, PIL; print('deps OK')"
Assert-ExitCode "依賴 import 驗證（若是 onnxruntime DLL load failed，請裝 Microsoft Visual C++ 2015-2022 Redistributable x64）"

.\.venv\Scripts\python -m pytest --version
Assert-ExitCode "pytest 可用性驗證（requirements.lock 是否含 dev 依賴？）"

Write-Host "BOOTSTRAP: DONE（接著回 AGENT_DEPLOY.md Step 3）"
