# 診所照片與檢驗報告歸檔系統（架構專案）

> 以病人為中心的診所影像歸檔設計：同仁用自己的手機拍病灶照自動歸檔、檢驗所 LINE 報告在地端 OCR 自動整理——同一位病人的紀錄，永遠整合在同一個資料夾。照片與報告的處理、儲存全在診所地端，不上任何雲端；唯一離開診所的是加密的離地災難備援碟。

**狀態**：架構定案（2026-07-18）・整合層 v0 程式碼已併入（2026-07-19，`integration/`）
**同仁說明網頁**：<https://med95albert.github.io/clinic-photo-archive/>

## 這個 repo 是什麼

本 repo 含**架構文件**與**整合層 v0 程式碼**：

- `integration/` — 整合層 v0（watcher、OCR、歸檔判準、佇列／時間軸網頁；含測試套件與契約自測，跑法見其 README）

- [docs/architecture.md](docs/architecture.md) — 完整系統架構、歸類模式與設計決策記錄
- [docs/photo-sop.md](docs/photo-sop.md) — 病灶拍照「N0 錨定協定」同仁 SOP（一頁）
- [docs/index.html](docs/index.html) — 同仁說明網頁（即 GitHub Pages 首頁）
- [bench/RESULTS.md](bench/RESULTS.md) — 地端 OCR 可行性實測數據
- [bench/ocr_bench.py](bench/ocr_bench.py) — 可重跑的 OCR 基準測試腳本

## 架構一句話

拍照端採用開源工具 [ClinicSnap](https://github.com/leon80148/ClinicSnap)（手機掃 QR 拍照、健保卡本機辨識、照片直落診間電腦），我們在其資料夾輸出之上自建「整合層」：watcher 監看收件夾 → 地端 OCR 抽取身分證字號 → checksum＋生日雙驗證 → 以證號為主鍵歸檔 → SQLite 索引＋待人工確認佇列。細節見 `docs/architecture.md`。

## 使用方式

這是文件 repo，直接讀即可：

1. 線上看同仁說明網頁：<https://med95albert.github.io/clinic-photo-archive/>
2. 本機看網頁：用任何瀏覽器開啟 `docs/index.html`
3. 讀架構文件：`docs/architecture.md`（GitHub 上可直接閱讀，含 mermaid 流程圖）

### （選配）重跑 OCR 基準測試

需要 Python 3.11+，並在本機另外 clone ClinicSnap（本 repo 不包含其程式碼）：

```bash
git clone https://github.com/leon80148/ClinicSnap.git
git -C ClinicSnap checkout f54a01a
python3 -m venv .venv
.venv/bin/pip install rapidocr onnxruntime pillow
.venv/bin/python bench/ocr_bench.py --clinicsnap-src ClinicSnap/src
```

腳本會合成檢驗報告樣式的測試影像（單一版面模板 × 多組欄位值 × 四種品質劣化變體），呼叫 ClinicSnap 的辨識模組量測欄位抽取率與單張耗時，結尾自報總測試張數。可重現性注意：腳本引用上游模組的內部符號，故指令鎖定在實測所用的 commit（`f54a01a`，v0.1.2）；rapidocr／onnxruntime 未鎖版，不同版本的次要欄位（姓名）命中數可能有 ±1 的差異。歷史實測快照與解讀見 [bench/RESULTS.md](bench/RESULTS.md)。

## 上游依賴與授權注意

- [ClinicSnap](https://github.com/leon80148/ClinicSnap)（作者 leon80148）：本 repo **不包含、不散布**其程式碼，僅以連結引用並在文件中描述其行為——該專案截至本文撰寫未附 LICENSE，改作或再散布前應徵得原作者同意。
- 本 repo 自有內容（文件、網頁、基準腳本）以 MIT 授權，全文見 `LICENSE`。

## 隱私聲明

本 repo 不含任何病人資料、不含診所內部帳號或金鑰。說明文件與示意圖中的身分證字號一律以後四碼遮罩（如 `A12345****`）呈現；`integration/` 的測試程式碼中出現的完整證號（如 `A123456789`）均為檢查碼演算法測試所需的公開慣用合成號碼，或於執行期隨機生成，非指涉任何真實個人。

<!-- cross-model-reviewed: 2026-07-18T15:58:13Z rounds=5 verdict=approved reviewer=codex:gpt-5.6-sol sha=76c4d5123f6956cd -->
