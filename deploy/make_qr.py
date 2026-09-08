#!/usr/bin/env python3
"""產生指向 TLS 反向代理的 ClinicSnap 手機 QR（deploy/TLS_EARLY.md §5）。

讀 ClinicSnap 的 config.json 取 token，組 https://<host>:<port>/?t=<token>，輸出 SVG。
刻意只印輸出檔路徑：token 與完整網址不得出現在主控台（會進現場 agent 的對話）。

依賴：segno（純 Python）。用法：
    python make_qr.py --config <ClinicSnap config.json> --host 192.168.1.21 --port 8443 --out qr_https.svg
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode


def build_url(host: str, port: int, token: str) -> str:
    # 與上游 _lan_url 相同的 query 形式（?t=token），只換 scheme 與 port。
    return f"https://{host}:{port}/?{urlencode({'t': token})}"


def read_token(config_path: Path) -> str:
    raw = config_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        # ClinicSnap 自己不吃 BOM；有 BOM 代表這份 config 早就被上游判為損毀重建過，先修 Step 4。
        raise SystemExit("config.json 帶 UTF-8 BOM，ClinicSnap 不會讀這份設定；先回 AGENT_DEPLOY Step 4 修正。")
    data = json.loads(raw.decode("utf-8"))
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise SystemExit("config.json 沒有 token 欄位（ClinicSnap 至少要成功啟動一次）。")
    return token


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="產生 https 版 ClinicSnap 手機 QR（SVG）")
    p.add_argument("--config", required=True, help="ClinicSnap 的 config.json 路徑")
    p.add_argument("--host", required=True, help="伺服器固定 IP（與 Caddyfile 站名一致）")
    p.add_argument("--port", type=int, default=8443, help="Caddy 代理 ClinicSnap 的 https 埠（預設 8443）")
    p.add_argument("--out", required=True, help="輸出 SVG 路徑")
    args = p.parse_args(argv)

    try:
        import segno  # type: ignore
    except ImportError:
        print("缺少 segno：請先 pip install segno==1.6.6", file=sys.stderr)
        return 2

    token = read_token(Path(args.config))
    url = build_url(args.host, args.port, token)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    segno.make(url, error="m").save(str(out), scale=8, border=4)
    # 只印路徑：不印 url、不印 token。
    print(f"已輸出 QR：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
