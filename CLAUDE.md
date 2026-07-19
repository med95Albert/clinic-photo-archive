# CLAUDE.md — 本 repo 的 AI 工作指引

診所照片與檢驗報告歸檔系統：架構文件＋整合層 v0 程式碼。權威設計文件是
`docs/architecture.md`（經 5 輪跨模型審查定案）——任何實作、部署、修改與它衝突時，以它為準。

## 你現在在哪種場景？

1. **診所 Windows 伺服器上，被要求「安裝／部署」**
   → 完整照 `deploy/AGENT_DEPLOY.md` 執行（含鐵律、逐步驗證、驗收表）。不要自行發明部署步驟。
2. **開發／修改整合層**
   → 程式碼在 `integration/`（規格 `integration/SPEC.md`）。改動核心判準（predicate/batching/archiver）
   前先讀 architecture.md §4/§5；所有修改必須讓 `pytest tests -q` 全綠，新行為要補回歸測試。
3. **回答架構問題** → `docs/architecture.md`；同仁白話版是 `docs/index.html`。

## 鐵律（所有場景通用）

- **病人資料絕不入對話**：`clinic_data\` 下的 `archive/ review/ staging/ inbox/ trash/` 內容
  與 `clinic.db` 的病人列，一律不讀、不印。日誌檔可以讀。
- **fail-closed 是本系統的靈魂**：任何「不確定就先歸檔／先刪／先猜」的修改方向都是錯的，
  存疑一律進待確認佇列。
- 上游 [ClinicSnap](https://github.com/leon80148/ClinicSnap) 無 LICENSE：**不得**把其程式碼
  複製進本 repo；只透過資料夾介面互動、以官方 release 部署。
- ClinicSnap 的網際網路（tunnel）模式院內政策禁用。
- 密碼／token（`FIRST_RUN_ADMIN.txt`、ClinicSnap config 的 token）不印進對話。

## 常用指令（integration/ 內）

```
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Windows；macOS 用 .venv/bin/
.venv/Scripts/python -m pytest tests -q                          # 全套測試
.venv/Scripts/python -m pytest tests/test_ocr_live.py -m e2e -q  # 真模型 OCR（首次下載模型）
.venv/Scripts/python contract_test.py --selftest                 # ClinicSnap 行為契約
python run.py                                                    # 啟動（config 與 clinic_data 落在目前工作目錄）
```
