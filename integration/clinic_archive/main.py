"""程式進入點（SPEC §10 的 main.py 部分）。

啟動序：載入設定 → 建立工作資料夾 → 初始化 DB → 確保初始管理員 → 起 watcher 執行緒
→ 於同一行程以 uvicorn 起 web；收到 SIGINT/SIGTERM 優雅停止。

刻意的延遲 import（整合注意 5）：``webapp.create_app`` 與 ``uvicorn`` 只在真的要起 web 時
才在 ``run()`` 內 import——讓 ``main`` 模組本身可被匯入而不依賴 T6 的 webapp，
使 e2e 測試能只驗 process/watcher 而不啟動 web。
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import threading
from pathlib import Path

from . import archiver, auth, config, db, watcher

logger = logging.getLogger(__name__)

LOG_FILENAME = "integration.log"


def _setup_logging(cfg) -> None:
    """設定 rotating 檔案 log（integration.log）＋ console，UI／log 一律繁中訊息。"""
    log_path = Path(cfg.data_root) / LOG_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)


def bootstrap(config_path: str | Path):
    """載入設定並完成一次性初始化，回傳 cfg。啟動連線用完即關（web／watcher 各自另開）。

    Fix-C：log 設定移到本函式最前面（原本在 ``run()`` 裡、於 ``bootstrap()`` 之後
    才呼叫），讓 init_db／初始管理員／開機耐久性巡檢（``reconcile``）期間的 log
    確實有 handler 可寫，不會因為還沒掛上 handler 而被靜默吞掉——尤其
    ``reconcile()`` 的孤兒檔統計，若沒人看得到就失去「開機耐久性巡檢」的意義。
    """
    cfg = config.load_config(config_path)
    config.ensure_dirs(cfg)
    _setup_logging(cfg)
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
        auth.ensure_initial_admin(conn, cfg.data_root)
        # 開機耐久性巡檢（Fix-C P1：搬檔後崩潰＝孤兒檔）：DB／佇列已就緒，
        # 在 watcher／web 開始跑之前先掃一輪，把上次非正常關機留下的孤兒檔
        # 掛回 queue_items 供人工複核。
        counts = archiver.reconcile(conn, cfg)
        logger.info(
            "開機耐久性巡檢完成：歸檔區孤兒檔 %d 筆、索引遺失檔 %d 筆、待確認區孤兒檔 %d 筆",
            counts.get("archive_orphan_files", 0),
            counts.get("archive_missing_records", 0),
            counts.get("review_orphan_files", 0),
        )
    finally:
        conn.close()
    return cfg


def run(config_path: str | Path = "config.json") -> None:
    """完整啟動：watcher 背景執行緒 + uvicorn web（同行程）；SIGINT/SIGTERM 優雅停。

    log 設定已移入 ``bootstrap()`` 最前面（見該函式 docstring），此處不再重複呼叫。
    """
    cfg = bootstrap(config_path)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=watcher.watcher_loop, args=(cfg, stop_event), name="watcher", daemon=True
    )
    thread.start()

    def _handle_signal(signum, _frame):
        logger.info("收到中止訊號 %s，準備優雅停止…", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        import uvicorn  # 延遲 import：讓 main 可在無 web 依賴時被匯入

        from clinic_archive.webapp import create_app  # T6，介面已約定

        app = create_app(cfg)
        logger.info("web 服務啟動於 http://%s:%s", cfg.web_host, cfg.web_port)
        uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="info")
    finally:
        # uvicorn.run 返回（含 SIGINT 觸發的優雅關閉）後，確保 watcher 一併收束。
        stop_event.set()
        thread.join(timeout=5)
        logger.info("整合層已停止")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="診所照片與檢驗報告歸檔整合層 v0")
    parser.add_argument(
        "--config", default="config.json", help="設定檔路徑（不存在則建立預設並寫回）"
    )
    args = parser.parse_args(argv)
    run(args.config)


if __name__ == "__main__":
    main()
