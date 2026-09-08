"""redact：log／診斷輸出的證號遮罩（跨模型審查 2026-09-09 P1：runbook 允許 agent 讀 log）。"""

from __future__ import annotations

import logging

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
        ("/data/archive/P-0000123/x.jpg", "/data/archive/P-000****/x.jpg"),
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
    assert len(out) == 8 + len(".jpg")
    assert redact.safe_name("王小明_檢驗報告.JPG") == out  # 穩定，可跨次對照


def test_masking_formatter_masks_args_and_exception_text():
    fmt = redact.MaskingFormatter("%(message)s")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "搬移 %s 失敗", ("archive/A123456789/x.jpg",), None)
    assert fmt.format(rec) == "搬移 archive/A12345****/x.jpg 失敗"
    try:
        raise RuntimeError("path archive/C345678901 locked")
    except RuntimeError:
        import sys
        rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = fmt.format(rec2)
    assert "C345678901" not in out and "C34567****" in out


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
