"""背景巡檢執行緒（SPEC §10 的 watcher.py 部分）。

每 ``poll_seconds`` 跑一輪 ``process_staging`` + ``process_inbox``；單輪任何例外都
記 log 但不中斷迴圈（fail-safe，避免一顆壞檔讓整條巡檢停擺）。

執行緒安全（整合注意 1）：sqlite 連線不可跨執行緒共用，故 ``watcher_loop`` 於本執行緒
內**自開自的連線**、迴圈結束才關閉，絕不與 web 端共用同一 connection。
"""

from __future__ import annotations

import logging
import threading

from . import db, reports

logger = logging.getLogger(__name__)


def run_once(conn, cfg, now: float | None = None) -> dict:
    """跑一輪巡檢（staging + inbox），回傳兩者計數。

    抽成獨立函式讓測試能以單一連線同執行緒精確驗一輪結果（免起真執行緒、免 sleep）。
    """
    staging_counts = reports.process_staging(conn, cfg, now=now)
    inbox_counts = reports.process_inbox(conn, cfg, now=now)
    return {"staging": staging_counts, "inbox": inbox_counts}


def watcher_loop(cfg, stop_event: threading.Event, poll_seconds: float | None = None) -> None:
    """背景巡檢迴圈：直到 ``stop_event`` 被設立才退出。

    於本執行緒自開連線並 init_db（idempotent，防被單獨啟動時表尚未建立）。
    ``poll_seconds`` 預設取 ``cfg.poll_seconds``，測試可縮短以加速。
    """
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    interval = cfg.poll_seconds if poll_seconds is None else poll_seconds
    logger.info("watcher 啟動，poll=%s 秒", interval)
    try:
        while not stop_event.is_set():
            try:
                run_once(conn, cfg)
            except Exception:
                logger.exception("watcher 本輪發生例外，已略過，不中斷巡檢")
            stop_event.wait(interval)
    finally:
        conn.close()
        logger.info("watcher 已停止")
