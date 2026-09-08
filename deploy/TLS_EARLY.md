# TLS 反向代理提前方案（architecture §8 觸發條件成立時啟用）

> 狀態：**設計完成、逐條對照上游原始碼、未在真 Windows 機驗證**。裝機日由現場 Claude Code 依本文逐步做並逐步驗證；任何一步與本文不符 → 停下回報，不猜。
> 先讀 `AGENT_DEPLOY.md` 的鐵律；本文是它的附錄，路徑與慣例沿用。

## 0. 何時啟用

architecture §8 的 v0 四項傳輸控制（員工 SSID WPA3-SAE／AP client isolation 實測／路由器 ACL／伺服器有線）**任一不成立**就啟用本方案，而且**兩個服務一起包**（ClinicSnap 8756＋整合層 8770），不得只包一個。診所現況是消費級 Linksys mesh（多半無 WPA3、無隔離），所以請把本方案當成**預設會做**，不是備案。

啟用後 `AGENT_DEPLOY.md` Step 6a 的網路三控制可改勾「TLS 提前方案驗證清單（本文 §7）全過」；BitLocker 與 admin 改密刪檔仍必須。

## 1. 設計依據（逐條對應上游 ClinicSnap v0.1.2 原始碼，唯讀、不改碼）

| 上游事實 | 對本方案的意義 |
|---|---|
| ClinicSnap 綁 `0.0.0.0:{port}`（預設 8756） | 不能只靠它「不對外聽」，要用主機防火牆擋 |
| QR 網址寫死 `http://{selectedIp}:{port}/?t={token}`，scheme／port 不可設，無 publicUrl | **視窗上的 QR 不能用**；要自己產一張指向 https 代理埠的 QR（§5） |
| 手機端網頁的 API 呼叫全是相對路徑 `/api/...`；拍照用 `<input capture="environment">` | 經反向代理不需改任何前端 |
| `/api/desktop/*`（改 saveDir、改設定、看最近上傳）由 `require_desktop` 守：只准 loopback 對端，**且請求帶 `cf-connecting-ip` 標頭就一律 403**（為 cloudflared 設計） | 代理跑在同一台機器，所有轉發請求的對端都是 127.0.0.1——**若不加這個標頭，桌面管理 API 就開給全診所手機**。所以代理必須對每個轉發請求加 `cf-connecting-ip` |
| 整合層綁 `0.0.0.0:8770`，無 loopback 限制路由 | 同樣用防火牆擋直連；經代理可正常用 |

## 2. 拓撲

```
員工手機 ──https 8443──▶ Caddy ──http 127.0.0.1:8756──▶ ClinicSnap（+ cf-connecting-ip → 桌面 API 403）
員工手機 ──https 8444──▶ Caddy ──http 127.0.0.1:8770──▶ 整合層佇列／時間軸
                         ▲ 只開 8443/8444 入站（private,domain）；8756/8770 不再有入站允許規則
伺服器本機視窗 ──http 127.0.0.1:8756──▶ ClinicSnap 桌面 UI（不經代理，loopback 不受防火牆過濾）
```

憑證：Caddy `tls internal` 自簽根 CA；站名用伺服器**固定 IP**（診所 DNS／mDNS 不可靠），根憑證裝到每支員工手機一次。

## 3. 安裝 Caddy（PowerShell 5.1）

```powershell
winget install --id CaddyServer.Caddy -e --accept-source-agreements --accept-package-agreements
caddy version   # 必須印出版本；沒有 → 重開 PowerShell 視窗（PATH 尚未刷新）再試
```

winget 找不到時：到 https://github.com/caddyserver/caddy/releases 下載 `caddy_<版本>_windows_amd64.zip`，解到 `C:\Caddy\caddy.exe`，下文的 `caddy` 改成完整路徑。下載前把檔名與大小告訴人類。

## 4. Caddyfile（`C:\ClinicArchive\Caddyfile`，**ASCII、無 BOM**，寫法比照 Step 4 的 `[IO.File]::WriteAllText`）

把 `192.168.1.21` 換成伺服器實際固定 IP（見人類提供的現場網路筆記；**IP 不入對話以外的任何檔案**——寫進 Caddyfile 是必要的，Caddyfile 不進 repo）。

```
{
    auto_https disable_redirects
}

# 手機拍照端（ClinicSnap）
https://192.168.1.21:8443 {
    tls internal
    reverse_proxy 127.0.0.1:8756 {
        # 上游 require_desktop 看到這個標頭一律 403：把桌面管理 API 擋在代理之外（§1）
        header_up cf-connecting-ip {remote_host}
    }
}

# 整合層佇列／時間軸
https://192.168.1.21:8444 {
    tls internal
    reverse_proxy 127.0.0.1:8770
}
```

**刻意不開 Caddy 存取 log**：存取 log 會把 QR 的 `?t=token` 與 `/p/<證號>` 這類網址明碼寫進磁碟，等於自己製造一個未遮罩的病人資料檔＋金鑰檔。排錯用 Caddy 的 stderr（下面的 bat 視窗）就夠；**不得**為了方便加 `log` 指令。

驗證設定檔語法：`caddy validate --config C:\ClinicArchive\Caddyfile`（必須印 `Valid configuration`）。

## 5. 產生自己的 QR（token 不入對話）

ClinicSnap 視窗上的 QR 永遠指向 `http://…:8756`，不能貼。用 repo 內 `deploy\make_qr.py`：讀 ClinicSnap 的 `config.json` 取 token、組 `https://<IP>:8443/?t=<token>`、輸出 SVG——**只印輸出檔路徑，不印網址、不印 token**。

```powershell
Set-Location C:\ClinicArchive\repo\integration
.\.venv\Scripts\python -m pip install "segno==1.6.6"     # 純 Python、無原生依賴；只有這支工具用
.\.venv\Scripts\python ..\deploy\make_qr.py --config (Join-Path $exeDir "config.json") --host 192.168.1.21 --port 8443 --out C:\ClinicArchive\qr_https.svg
```

印出 `C:\ClinicArchive\qr_https.svg`（瀏覽器開啟後列印），**只張貼於員工區**；ClinicSnap 視窗裡的 QR 從此不用。ClinicSnap 桌面設定裡的「重新產生 token」按了 QR 就作廢，要重跑本步——寫進交接給人類的注意事項。

## 6. 防火牆與開機自啟

```powershell
# 先確認連線設定檔是 Private/Domain（同 Step 4）
Get-NetConnectionProfile | Select-Object Name, NetworkCategory
# 移除舊的兩條入站允許（8756/8770 從此只准 loopback；loopback 不受 Windows 防火牆過濾）
netsh advfirewall firewall delete rule name="ClinicSnap 8756"
netsh advfirewall firewall delete rule name="ClinicArchive Web 8770"
# 只開代理埠
netsh advfirewall firewall add rule name="Caddy TLS 8443" dir=in action=allow protocol=TCP localport=8443 profile=private,domain
netsh advfirewall firewall add rule name="Caddy TLS 8444" dir=in action=allow protocol=TCP localport=8444 profile=private,domain
```

開機自啟：比照 Step 5c 的做法，新增 `C:\ClinicArchive\start_caddy.bat`（內容一行：`caddy run --config C:\ClinicArchive\Caddyfile`，caddy 不在 PATH 就寫完整路徑），放一個捷徑到同一個「啟動」資料夾。Caddy 要在整合層之後起也沒關係（後端沒起只會 502，起來就通）。

## 7. 根憑證佈到員工手機（一次性）

Caddy 首次以 `tls internal` 啟動後，根憑證在執行 Caddy 的那個 Windows 帳號底下：

```powershell
$root = Join-Path $env:APPDATA "Caddy\pki\authorities\local\root.crt"
Test-Path $root      # True；False＝Caddy 還沒成功起過，先看 start_caddy.bat 視窗的錯誤
Copy-Item $root C:\ClinicArchive\clinic-root-ca.crt
```

`clinic-root-ca.crt` 是**公開的憑證**（沒有私鑰），用 LINE／AirDrop／USB 傳給每支員工手機都可以。

- **iPhone**：開啟 .crt → 設定 → 一般 → VPN 與裝置管理 → 安裝描述檔 → 再到 設定 → 一般 → 關於本機 → **憑證信任設定** → 把該根憑證的「完全信任」打開（少了這一步 Safari 仍會擋）。
- **Android**：設定 → 安全性（或「密碼與安全性」）→ 加密與憑證 → 安裝憑證 → **CA 憑證** → 選檔 → 「仍要安裝」。Chrome 信任使用者安裝的 CA；手機端是網頁，不涉及不信任使用者 CA 的原生 App。

**誠實揭露**：手機一旦「完全信任」這顆根 CA，它就能對該手機簽**任何網站**的憑證；根 CA 的私鑰在伺服器 `%APPDATA%\Caddy\pki\authorities\local\root.key`。伺服器本來就是全診所病人資料所在（BitLocker＋ACL 已是前提），這裡沒有新增更高價值的目標；但伺服器若確認遭入侵，除了病人資料應變，**還要**在每支手機移除這顆根憑證並重發（記入資安事件演練清單）。Caddy 內建 CA 不支援 name constraints，無法把信任範圍限縮到診所 IP——這是接受的已知限制，v1 若上游提供原生 HTTPS 即可退場。

## 8. 驗證清單（全過才算 TLS 提前完成；結果貼進完成報告）

在伺服器上先讓 PowerShell 信任這顆 CA，才能用 `Invoke-WebRequest` 測（需 UAC，先告知人類）：`caddy trust`。

| # | 測什麼 | 指令／動作 | 過關 |
|---|---|---|---|
| 1 | 代理在聽 | `Test-NetConnection 127.0.0.1 -Port 8443`、`-Port 8444` | 皆 True |
| 2 | ClinicSnap 手機頁經代理可達 | `(Invoke-WebRequest -UseBasicParsing https://192.168.1.21:8443/).StatusCode` | 200 |
| 3 | **桌面 API 被代理擋住** | `try { Invoke-WebRequest -UseBasicParsing https://192.168.1.21:8443/api/desktop/state } catch { $_.Exception.Response.StatusCode.value__ }` | **403**（不是 200；200＝忘了 `header_up cf-connecting-ip`，立刻修） |
| 4 | 桌面視窗本機直連仍正常 | `(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8756/api/desktop/state).StatusCode` | 200 |
| 5 | 整合層經代理可達 | `(Invoke-WebRequest -UseBasicParsing https://192.168.1.21:8444/login).StatusCode` | 200 |
| 6 | **明文埠對區網已關** | 用一支員工手機（已連員工 Wi-Fi）開 `http://192.168.1.21:8756/` 與 `http://192.168.1.21:8770/` | 皆連不上（逾時／拒絕） |
| 7 | 手機走 https 全程可用 | 手機掃 §5 印出的 QR → 無憑證警告 → 拍 2 張白紙（不帶代碼、不拍卡）送出 | 送出成功；`contract_test.py <staging> --config …` PASS |
| 8 | 手機登整合層 | 手機開 `https://192.168.1.21:8444` → 登入 | 無憑證警告、登入成功 |
| 9 | 重開機 | 重開 → 1、3、6 重測 | 皆通過 |

第 3、6 兩項是安全底線：任一沒過，**不得**讓真病人資料經過系統，回報 Albert。

## 9. 交接給人類的注意事項

- 新同仁手機：裝根憑證（§7）＋掃員工區 QR；不必再連 ClinicSnap 視窗。
- 換伺服器 IP、重灌 Caddy、或按了 ClinicSnap「重新產生 token」：QR 要重印（§5）；重灌 Caddy 還要重發根憑證（§7）。
- Caddy 的 stderr 視窗可能出現手機 IP，那不是病人資料；**不要**為了排錯開存取 log（§4）。
