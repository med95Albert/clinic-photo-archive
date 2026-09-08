"""redact：log／診斷輸出的證號遮罩（跨模型審查 2026-09-09 P1：runbook 允許 agent 讀 log）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from clinic_archive import main, redact


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("A123456789", "A12345****"),
        ("證號A123456789已建檔", "證號A12345****已建檔"),
        ("A123456789|2026-07-18|101530", "A12345****|2026-07-18|101530"),
        (r"C:\ClinicArchive\clinic_data\archive\B234567890\2026-07-18_101530_1.jpg",
         r"C:\ClinicArchive\clinic_data\archive\B23456****\2026-07-18_101530_1.jpg"),
        ("/data/archive/P-0000123/2026-07-18_101530_1.jpg", "/data/archive/P-000****/2026-07-18_101530_1.jpg"),
        ("GET /p/A123456789 HTTP/1.1 200", "GET /p/A12345**** HTTP/1.1 200"),
        # 更長的英數編號不是證號，不切一段出來誤遮；純數字亦不動
        ("AB1234567890", "AB1234567890"),
        ("2026071800123", "2026071800123"),
        ("", ""),
    ],
)
def test_mask_text(raw, expected):
    assert redact.mask_text(raw) == expected


def test_mask_pid_none_and_pcode():
    assert redact.mask_pid(None) == ""
    assert redact.mask_pid("P-1234567") == "P-123****"


def test_safe_name_hides_user_chosen_filename_but_keeps_suffix():
    out = redact.safe_name("王小明_檢驗報告.JPG")
    assert out.endswith(".jpg")
    assert "王小明" not in out
    assert len(out) == 1 + 8 + len(".jpg")   # h + 雜湊 + 副檔名
    assert redact.safe_name("王小明_檢驗報告.JPG") == out  # 穩定，可跨次對照


def test_masking_formatter_masks_args_and_exception_text():
    fmt = redact.MaskingFormatter("%(message)s")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "搬移 %s 失敗", ("archive/A123456789/2026-07-18_101530_1.jpg",), None)
    assert fmt.format(rec) == "搬移 archive/A12345****/2026-07-18_101530_1.jpg 失敗"
    try:
        raise RuntimeError("path archive/C345678901 locked")
    except RuntimeError:
        import sys
        rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = fmt.format(rec2)
    assert "C345678901" not in out and "C34567****" in out


@pytest.mark.parametrize(
    "raw, must_not_contain, must_contain",
    [
        # 使用者取的檔名（含姓名）在工作資料夾下 → 雜湊、留副檔名；POSIX 與 Windows 皆同
        ("/data/clinic_data/review/王小明_檢驗報告.jpg", "王小明", "/review/h"),
        (r"C:\ClinicArchive\clinic_data\inbox\王小明.JPG", "王小明", r"\inbox\h"),
        # LINE 預設檔名也換掉（非系統產生）
        ("inbox/S__12345678.jpg", "S__12345678", "inbox/h"),
        # 系統產生的名字保留：證號資料夾（已遮罩）＋時間戳檔名、_unsorted、日期資料夾
        ("archive/A123456789/2026-07-18_101530_1-2.jpg", "A123456789", "archive/A12345****/2026-07-18_101530_1-2.jpg"),
        ("staging/_unsorted/2026-07-18_101530_3.jpg", "h", "staging/_unsorted/2026-07-18_101530_3.jpg"),
        # uvicorn 存取 log 格式：query 值一律 <redacted>（病人搜尋字串＝姓名）
        ('127.0.0.1:50000 - "GET /patients?q=%E7%8E%8B%E5%B0%8F%E6%98%8E HTTP/1.1" 200', "%E7%8E%8B", "q=<redacted>"),
        ("GET /p/A123456789?tab=x&q=王小明", "王小明", "/p/A12345****?tab=<redacted>&q=<redacted>"),
    ],
)
def test_mask_text_paths_and_queries(raw, must_not_contain, must_contain):
    out = redact.mask_text(raw)
    assert must_not_contain not in out, out
    assert must_contain in out, out


def test_mask_text_path_hash_is_stable_and_keeps_suffix():
    a = redact.mask_text("review/王小明_檢驗報告.jpg")
    b = redact.mask_text("trash/王小明_檢驗報告.jpg")
    assert a.split("/")[-1] == b.split("/")[-1] == redact.safe_name("王小明_檢驗報告.jpg")
    assert a.endswith(".jpg")


def test_mask_segment_bare_names():
    assert redact.mask_segment("2026-07-18_101530_1.jpg") == "2026-07-18_101530_1.jpg"
    assert redact.mask_segment("_unsorted") == "_unsorted"
    assert redact.mask_segment("A123456789") == "A12345****"
    out = redact.mask_segment("王小明.jpg")
    assert "王小明" not in out and out.startswith("h") and out.endswith(".jpg")


def test_masking_formatter_sanitizes_traceback_with_named_inbox_file():
    fmt = redact.MaskingFormatter("%(message)s")
    try:
        raise FileNotFoundError(2, "No such file", "/srv/clinic_data/inbox/王小明_檢驗報告.jpg")
    except FileNotFoundError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "inbox OCR／解析失敗：%s",
                                (redact.safe_name("王小明_檢驗報告.jpg"),), sys.exc_info())
    out = fmt.format(rec)
    assert "王小明" not in out
    assert "/inbox/h" in out


def test_redact_cli_sanitizes_file(tmp_path):
    import subprocess
    import sys

    log = tmp_path / "clinic_snap.log"
    log.write_text(
        "2026-07-18 INFO uploaded staging/A123456789/2026-07-18_101530_1.jpg\n"
        "2026-07-18 ERROR failed inbox/王小明.jpg\n"
        "2026-07-18 INFO GET /patients?q=王小明\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "clinic_archive.redact", str(log), "--tail", "2"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    assert "王小明" not in proc.stdout
    assert "A123456789" not in proc.stdout
    assert proc.stdout.count("\n") == 2       # --tail 2
    assert "q=<redacted>" in proc.stdout and "inbox/h" in proc.stdout


def test_run_disables_uvicorn_access_log(tmp_path, monkeypatch):
    """存取 log 會把 /patients?q=<姓名> 逐筆寫盤：run() 必須 access_log=False、log_config=None。"""
    import json
    import types

    from clinic_archive import config, main as main_mod, watcher

    cfg = config.AppConfig(data_root=str(tmp_path / "data"))
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"data_root": cfg.data_root, "db_path": str(tmp_path / "data" / "clinic.db")}), encoding="utf-8")

    captured = {}
    fake_uvicorn = types.SimpleNamespace(run=lambda app, **kw: captured.update(kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    fake_webapp = types.SimpleNamespace(create_app=lambda cfg: object())
    monkeypatch.setitem(sys.modules, "clinic_archive.webapp", fake_webapp)
    monkeypatch.setattr(watcher, "watcher_loop", lambda cfg, stop: None)
    root = logging.getLogger(); before = list(root.handlers)
    try:
        main_mod.run(cfg_path)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h); h.close()
    assert captured.get("access_log") is False
    assert captured.get("log_config") is None


def test_setup_logging_installs_masking_formatter(tmp_path):
    import dataclasses

    from clinic_archive import config

    cfg = dataclasses.replace(config.AppConfig(data_root=str(tmp_path)))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        main._setup_logging(cfg)
        added = [h for h in root.handlers if h not in before]
        assert added, "應該掛上 handler"
        assert all(isinstance(h.formatter, redact.MaskingFormatter) for h in added)
        logging.getLogger("uvicorn.access").info('GET /p/A123456789 200')
        for h in added:
            h.flush()
        text = (tmp_path / main.LOG_FILENAME).read_text(encoding="utf-8")
        assert "A123456789" not in text and "A12345****" in text
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
