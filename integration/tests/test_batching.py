"""batching.scan_staging 的確定性混批偵測測試
（architecture §5 第 7 條（批次還原機制）/ SPEC §7）。

以 os.utime 固定 mtime、以 now 參數控制靜置窗，測試不依真實時鐘。
"""

import os

import pytest

from clinic_archive.batching import (
    REASON_BAD_NAME,
    REASON_MIXED,
    REASON_STRAGGLER,
    BatchGroup,
    scan_staging,
)

BASE = 1_700_000_000.0          # 固定基準 mtime（epoch 秒）
SETTLED = BASE + 3600.0         # 遠超靜置窗 → 視為已靜置
FRESH = BASE + 1.0             # 未滿 settle_seconds → 未靜置


def mkfile(root, relpath, mtime=BASE, data=b"x"):
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


def by_key(groups):
    return {g.key: g for g in groups}


def test_normal_batch_complete(tmp_path):
    staging = tmp_path / "staging"
    for i in (1, 2, 3):
        mkfile(staging, f"A123456789/2026-07-18_101500_{i}.jpg")
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    g = groups[0]
    assert isinstance(g, BatchGroup)
    assert g.key == "A123456789|2026-07-18|101500"
    assert g.pid == "A123456789"
    assert g.complete is True
    assert g.suspect_reason is None
    assert len(g.files) == 3
    # 檔案依 idx 排序
    assert [p.name for p in g.files] == [
        "2026-07-18_101500_1.jpg",
        "2026-07-18_101500_2.jpg",
        "2026-07-18_101500_3.jpg",
    ]


def test_unsettled_batch_skipped(tmp_path):
    staging = tmp_path / "staging"
    for i in (1, 2):
        mkfile(staging, f"A123456789/2026-07-18_101500_{i}.jpg", mtime=FRESH)
    # now 距最新 mtime 僅 ~ (FRESH-BASE 之外還要看 now)；用 now=FRESH+1 < settle 10
    groups = scan_staging(staging, FRESH + 1.0)
    assert groups == []  # 靜置窗未過 → 本輪不回傳


def test_settle_boundary_inclusive(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg", mtime=BASE)
    # now - mtime == settle_seconds → 已達門檻，回傳
    assert scan_staging(staging, BASE + 10.0, settle_seconds=10) != []
    # now - mtime == 9 < 10 → 跳過
    assert scan_staging(staging, BASE + 9.0, settle_seconds=10) == []


def test_collision_suffix_is_mixed(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg")
    mkfile(staging, "A123456789/2026-07-18_101500_2.jpg")
    mkfile(staging, "A123456789/2026-07-18_101500_2-2.jpg")  # 上游碰撞後綴
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].complete is False
    assert groups[0].suspect_reason == REASON_MIXED


def test_missing_index_is_mixed(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg")
    mkfile(staging, "A123456789/2026-07-18_101500_3.jpg")  # 缺 2
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].complete is False
    assert groups[0].suspect_reason == REASON_MIXED


def test_straggler_by_processed_key(tmp_path):
    staging = tmp_path / "staging"
    # 一個結構上完整的組，但鍵已在 processed_batches → 遲到檔
    for i in (1, 2):
        mkfile(staging, f"A123456789/2026-07-18_101500_{i}.jpg")
    key = "A123456789|2026-07-18|101500"
    groups = scan_staging(staging, SETTLED, processed_keys={key})
    assert len(groups) == 1
    assert groups[0].suspect_reason == REASON_STRAGGLER
    # 遲到檔即使結構完整仍進佇列（fail-closed）
    assert groups[0].complete is True


def test_straggler_precedence_over_mixed(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_3.jpg")  # 尾檔慢速寫入
    key = "A123456789|2026-07-18|101500"
    groups = scan_staging(staging, SETTLED, processed_keys={key})
    assert groups[0].suspect_reason == REASON_STRAGGLER  # 遲到檔優先於序號不連續


def test_unsorted_folder_pid_none(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "_unsorted/2026-07-18_090000_1.jpg")
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].pid is None
    assert groups[0].key == "~|2026-07-18|090000"
    assert groups[0].complete is True
    assert groups[0].suspect_reason is None


def test_scattered_file_pid_none(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "2026-07-18_090000_1.jpg")  # 直接散落在 staging 根
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].pid is None


def test_garbage_filename_own_group(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/隨手拍.jpg")
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].suspect_reason == REASON_BAD_NAME
    assert groups[0].complete is False
    assert len(groups[0].files) == 1


def test_multiple_batches_distinct_keys(tmp_path):
    staging = tmp_path / "staging"
    # 同一 pid，不同時分秒 → 兩批不相混
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg")
    mkfile(staging, "A123456789/2026-07-18_101500_2.jpg")
    mkfile(staging, "A123456789/2026-07-18_140000_1.jpg")
    groups = by_key(scan_staging(staging, SETTLED))
    assert set(groups) == {
        "A123456789|2026-07-18|101500",
        "A123456789|2026-07-18|140000",
    }
    assert all(g.suspect_reason is None for g in groups.values())


def test_different_pids_not_merged(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg")
    mkfile(staging, "B223456780/2026-07-18_101500_1.jpg")  # 同時分秒但不同 pid
    groups = by_key(scan_staging(staging, SETTLED))
    assert set(groups) == {
        "A123456789|2026-07-18|101500",
        "B223456780|2026-07-18|101500",
    }


def test_missing_staging_returns_empty(tmp_path):
    assert scan_staging(tmp_path / "nope", SETTLED) == []


def test_dotfiles_ignored(tmp_path):
    staging = tmp_path / "staging"
    mkfile(staging, "A123456789/2026-07-18_101500_1.jpg")
    mkfile(staging, "A123456789/.DS_Store")
    groups = scan_staging(staging, SETTLED)
    assert len(groups) == 1
    assert groups[0].complete is True  # 系統雜項不算入批
    assert len(groups[0].files) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
