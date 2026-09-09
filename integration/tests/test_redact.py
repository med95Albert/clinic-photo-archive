"""redact：log／診斷輸出的病人識別資料淨化（跨模型審查 2026-09-09 R1–R4）。"""

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
        ("GET /p/A123456789 HTTP/1.1 200", "GET /p/A12345**** HTTP/1.1 200"),
        # 更長的英數編號不是證號，不切一段出來誤遮；純數字亦不動
        ("AB1234567890", "AB1234567890"),
        ("2026071800123", "2026071800123"),
        ("", ""),
        # 未加引號的工作資料夾路徑：一律從 token 起遮到行尾（前綴長相不算邊界證據）
        (r"C:\ClinicArchive\clinic_data\archive\B234567890\2026-07-18_101530_1.jpg",
         r"C:\ClinicArchive\clinic_data\archive\<redacted>"),
        ("/data/archive/P-0000123/2026-07-18_101530_1.jpg", "/data/archive/<redacted>"),
        ("inbox/2026-09-09 王小明.jpg", "inbox/<redacted>"),                              # R4
        ("review/2026-09-09_101530_1.jpg 王小明.jpg", "review/<redacted>"),               # R4
        ("failed inbox/report 王小明.jpg then more", "failed inbox/<redacted>"),
        ("inbox/2026-09-09_王小明.jpg", "inbox/<redacted>"),
        ("收件夾inbox/王小明.jpg", "收件夾inbox/<redacted>"),
        ("uploaded staging/A123456789/2026-07-18_101530_1.jpg", "uploaded staging/<redacted>"),
        # 引號內路徑：邊界確定 → 逐段淨化，引號後文保留
        ("cannot identify image file '/srv/clinic_data/inbox/report 王小明.jpg' at all",
         "cannot identify image file '/srv/clinic_data/inbox/h" + redact.safe_name("report 王小明.jpg")[1:] + "' at all"),
        ("搬移 '/d/staging/A123456789/2026-07-18_101530_1.jpg' → '/d/archive/A123456789/2026-07-18_病灶照_01.jpg' 連續 3 次",
         "搬移 '/d/staging/A12345****/2026-07-18_101530_1.jpg' → '/d/archive/A12345****/2026-07-18_病灶照_01.jpg' 連續 3 次"),
        ('"/d/archive/A123456789/2026-07-18_檢驗-CBC_01-2.png" ok', '"/d/archive/A12345****/2026-07-18_檢驗-CBC_01-2.png" ok'),
        # Python repr 的 Windows 路徑（雙反斜線）在引號內
        ("[Errno 2] No such file: 'C:\\\\ClinicArchive\\\\clinic_data\\\\inbox\\\\王小明.jpg'",
         "[Errno 2] No such file: 'C:\\\\ClinicArchive\\\\clinic_data\\\\inbox\\\\" + redact.safe_name("王小明.jpg") + "'"),
        # uvicorn 存取 log 格式：query 值一律 <redacted>
        ('127.0.0.1:50000 - "GET /patients?q=%E7%8E%8B%E5%B0%8F%E6%98%8E HTTP/1.1" 200',
         '127.0.0.1:50000 - "GET /patients?q=<redacted> HTTP/1.1" 200'),
        ("GET /p/A123456789?tab=x&q=王小明", "GET /p/A12345****?tab=<redacted>&q=<redacted>"),
        # 引號內路徑與未加引號路徑同行：引號的保留、未加引號的遮到行尾
        ("src 'inbox/王.jpg' dst staging/A123456789/x.jpg tail", "src 'inbox/" + redact.safe_name("王.jpg") + "' dst staging/<redacted>"),
    ],
)
def test_mask_text(raw, expected):
    out = redact.mask_text(raw)
    assert out == expected, out
    assert redact.mask_text(out) == out   # 冪等


def test_mask_text_multiline_independent():
    out = redact.mask_text("第一行 review/王 小明 報告.jpg → archive/x 後文\n第二行 沒有路徑")
    first, second = out.split("\n")
    assert first == "第一行 review/<redacted>"
    assert second == "第二行 沒有路徑"


def test_mask_path_full_value_boundary():
    assert redact.mask_path("/data/archive/A123456789/2026-07-18_病灶照_01.jpg") == "/data/archive/A12345****/2026-07-18_病灶照_01.jpg"
    assert redact.mask_path(r"C:\ClinicArchive\clinic_data\staging\_unsorted\2026-07-18_101530_3.jpg") == r"C:\ClinicArchive\clinic_data\staging\_unsorted\2026-07-18_101530_3.jpg"
    out = redact.mask_path("/data/review/王小明 報告.jpg")
    assert out == "/data/review/" + redact.safe_name("王小明 報告.jpg")
    assert redact.mask_path("/data/review/2026-09-09 王小明.jpg") == "/data/review/" + redact.safe_name("2026-09-09 王小明.jpg")
    assert redact.mask_path("C:/ClinicArchive/config.json") == "C:/ClinicArchive/config.json"   # 非工作資料夾：不動
    assert redact.mask_path(Path("/x/inbox/王.jpg")) == "/x/inbox/" + redact.safe_name("王.jpg")


def test_mask_pid_none_and_pcode():
    assert redact.mask_pid(None) == ""
    assert redact.mask_pid("P-1234567") == "P-123****"


def test_safe_name_hides_user_chosen_filename_but_keeps_suffix():
    out = redact.safe_name("王小明_檢驗報告.JPG")
    assert out.endswith(".jpg") and "王小明" not in out
    assert len(out) == 1 + 8 + len(".jpg")
    assert redact.safe_name("王小明_檢驗報告.JPG") == out


def test_mask_segment_bare_names():
    assert redact.mask_segment("2026-07-18_101530_1.jpg") == "2026-07-18_101530_1.jpg"
    assert redact.mask_segment("2026-07-18_病灶照_01.jpg") == "2026-07-18_病灶照_01.jpg"
    assert redact.mask_segment("_unsorted") == "_unsorted"
    assert redact.mask_segment("A123456789") == "A12345****"
    assert redact.mask_segment("2026-09-09_王小明.jpg").startswith("h")
    assert redact.mask_segment("2026-09-09 王小明.jpg").startswith("h")
    out = redact.mask_segment("王小明.jpg")
    assert "王小明" not in out and out.startswith("h") and out.endswith(".jpg")
    assert redact.mask_segment(out) == out


def test_masking_formatter_masks_args_and_exception_text():
    fmt = redact.MaskingFormatter("%(message)s")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "搬移 '%s' 失敗", (redact.mask_path("archive/A123456789/2026-07-18_101530_1.jpg"),), None)
    assert fmt.format(rec) == "搬移 'archive/A12345****/2026-07-18_101530_1.jpg' 失敗"
    try:
        raise RuntimeError("path archive/C345678901 locked")
    except RuntimeError:
        rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = fmt.format(rec2)
    assert "C345678901" not in out and "archive/<redacted>" in out


def test_masking_formatter_sanitizes_traceback_with_named_inbox_file():
    fmt = redact.MaskingFormatter("%(message)s")
    try:
        raise FileNotFoundError(2, "No such file", "/srv/clinic_data/inbox/王小明_檢驗報告.jpg")
    except FileNotFoundError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "inbox OCR／解析失敗：%s",
                                (redact.safe_name("王小明_檢驗報告.jpg"),), sys.exc_info())
    out = fmt.format(rec)
    assert "王小明" not in out and "/inbox/h" in out


def test_masking_formatter_windows_style_exception_repr():
    fmt = redact.MaskingFormatter("%(message)s")
    try:
        raise FileNotFoundError(2, "No such file or directory", r"C:\ClinicArchive\clinic_data\inbox\王小明 報告.jpg")
    except FileNotFoundError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "inbox 失敗", (), sys.exc_info())
    out = fmt.format(rec)
    assert "王小明" not in out and "inbox" in out


def test_redact_cli_sanitizes_file(tmp_path):
    import subprocess

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
    assert "王小明" not in proc.stdout and "A123456789" not in proc.stdout
    assert proc.stdout.count("\n") == 2
    assert "q=<redacted>" in proc.stdout and "inbox/<redacted>" in proc.stdout


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
        logging.getLogger("uvicorn.error").info('GET /p/A123456789 200')
        logging.getLogger("x").warning("move failed review/2026-09-09 王小明.jpg to archive")
        for h in added:
            h.flush()
        text = (tmp_path / main.LOG_FILENAME).read_text(encoding="utf-8")
        assert "A123456789" not in text and "A12345****" in text
        assert "王小明" not in text and "review/<redacted>" in text
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
