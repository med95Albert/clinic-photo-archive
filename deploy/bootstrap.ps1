# bootstrap.ps1 — 環境建置加速器（冪等；AGENT_DEPLOY.md Step 2 的腳本版）
# 假設 Python 3.11+ 與 Git 已在 PATH（沒有 → 先照 AGENT_DEPLOY Step 1 用 winget 安裝）。
# 本腳本刻意只做「建目錄＋clone/更新＋venv＋依賴」，其餘步驟由 agent 照 runbook 逐步驗證執行。

$ErrorActionPreference = "Stop"

python --version
git --version

New-Item -ItemType Directory -Force C:\ClinicArchive | Out-Null
Set-Location C:\ClinicArchive

if (Test-Path .\repo\.git) {
  git -C repo pull --ff-only
} else {
  git clone https://github.com/med95Albert/clinic-photo-archive.git repo
}

Set-Location repo\integration
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\pip install -e ".[dev]"

.\.venv\Scripts\python -c "import fastapi, rapidocr, onnxruntime, PIL; print('deps OK')"
Write-Host "BOOTSTRAP: DONE（接著回 AGENT_DEPLOY.md Step 3）"
