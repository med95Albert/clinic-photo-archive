#!/usr/bin/env python3
"""contract_test.py — 整合層與 ClinicSnap 之間的檔名／資料夾契約驗證工具（T7）。

背景：`clinic_archive.batching`（architecture.md 第 5 節、SPEC.md 第 7 節）假設
ClinicSnap（`archiveMode=by_patient`）輸出固定格式：

    staging/{病患代碼}/{YYYY-MM-DD}_{HHMMSS}_{idx}[-{碰撞後綴}].{ext}
    staging/_unsorted/{同格式}                       ← 掃不到代碼的批次

這支腳本獨立於 watcher 之外，直接對一份 staging 輸出（真實或自產樣本）驗證這個假設
是否仍然成立。**請在升級 ClinicSnap 版本前先跑一次**：如果上游改了命名規則，這支腳本
會在這裡就報 FAIL，而不是等 watcher 在正式環境裡把整批資料歸進待確認佇列才被發現。

用法
----
    python contract_test.py <staging_dir>                對真實 ClinicSnap staging 輸出驗證
    python contract_test.py --simulate                   自產一組模擬輸出到暫存目錄再驗證
    python contract_test.py <staging_dir> --config <ClinicSnap config.json>
                                                            額外檢查 ClinicSnap 的設定檔
    python contract_test.py --selftest                   跑 --simulate 並斷言結果為 PASS（供 CI）

四條契約（逐條印 PASS/FAIL＋證據行；只要有一條 FAIL，整體就是 CONTRACT: FAIL）：
    1. 所有檔名可被 `clinic_archive.batching.FILE_RE` 解析。
    2. 同一批（同資料夾＋同時間戳、且無碰撞後綴）序號恰為 1..N 連續。
    3. 碰撞後綴（同名衝突時上游附加的 -2、-3…）可被正確辨識，且不應出現不合理的 -1
       （上游慣例是第一份不加後綴，重複才從 -2 起算）。
    4. staging 第一層只能是「病患代碼資料夾」或 `_unsorted`：不可有落單根檔案，
       也不可有二層以上巢狀。

`--config` 檢查的是 ClinicSnap 自己的 config.json（token / saveDir / archiveMode），
跟上面四條「檔名格式契約」是兩件不同的事，所以刻意分開：不符合只印 WARN，不會讓
CONTRACT 判定變成 FAIL。

設計取捨：本工具驗證的是「某一瞬間的靜態快照」，因此**不套用生產環境的靜置窗**
（`settle_seconds`）——刻意看到全部檔案，不因為「剛寫入」而跳過任何一筆。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# 不管執行時的 cwd、也不管 venv 的 editable install 是否正常運作，都要能 import 到
# 本目錄下的 clinic_archive（contract_test.py 就放在 clinic_archive/ 的同層）。
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from clinic_archive.batching import FILE_RE  # noqa: E402


# ---------------------------------------------------------------------------
# 走訪 staging 目錄。故意不重用 clinic_archive.batching.scan_staging 的分組邏輯——
# 契約測試要驗的正是「檔名格式」與「資料夾結構」本身，用同一套程式碼驗自己意義不大。
# ---------------------------------------------------------------------------


@dataclass
class _Walk:
    top_dirs: list[Path] = field(default_factory=list)
    root_loose_files: list[Path] = field(default_factory=list)
    files_by_dir: dict[Path, list[Path]] = field(default_factory=dict)
    nested_subdirs: dict[Path, list[Path]] = field(default_factory=dict)


def _walk_staging(staging: Path) -> _Walk:
    """走訪 staging 一層：第一層是資料夾（病患代碼或 _unsorted）或落單檔案；
    每個資料夾內只期待檔案，若還有子資料夾另外記下（供契約 4 判定用）。
    跳過 dotfile（.DS_Store 等系統雜項），行為對齊 batching.scan_staging。
    """
    w = _Walk()
    if not staging.exists():
        return w
    for entry in sorted(staging.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            w.top_dirs.append(entry)
            files: list[Path] = []
            subdirs: list[Path] = []
            for child in sorted(entry.iterdir(), key=lambda p: p.name):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    subdirs.append(child)
                elif child.is_file():
                    files.append(child)
            w.files_by_dir[entry] = files
            if subdirs:
                w.nested_subdirs[entry] = subdirs
        elif entry.is_file():
            w.root_loose_files.append(entry)
    return w


@dataclass
class ClauseResult:
    label: str
    passed: bool
    evidence: list[str]


def _fmt_names(paths: list[Path], limit: int = 8) -> str:
    if not paths:
        return "（無）"
    names = [p.name for p in paths[:limit]]
    more = f"（其餘 {len(paths) - limit} 個略）" if len(paths) > limit else ""
    return "、".join(names) + more


# ---------------------------------------------------------------------------
# 契約 1：所有檔名可被 FILE_RE 解析
# ---------------------------------------------------------------------------


def _check_filenames_parseable(walk: _Walk) -> ClauseResult:
    all_files = list(walk.root_loose_files)
    for files in walk.files_by_dir.values():
        all_files.extend(files)
    bad = [f for f in all_files if not FILE_RE.match(f.name)]
    total = len(all_files)
    ok = total - len(bad)
    passed = not bad
    evidence = [f"共 {total} 個檔案，{ok} 個符合 FILE_RE、{len(bad)} 個不符合。"]
    if bad:
        evidence.append(f"不符合的檔名：{_fmt_names(bad)}")
    elif total == 0:
        evidence.append("（0 個檔案代表尚無資料可驗，視為未違反契約）")
    else:
        evidence.append("全數可解析。")
    return ClauseResult("① 所有檔名可被 FILE_RE 解析", passed, evidence)


# ---------------------------------------------------------------------------
# 共用：把單一資料夾內的檔案依 (date, time) 分組——分組鍵對齊 batching.py／
# architecture.md §5.6 的定義（同一上傳請求共用同一時間戳）。只有契約 2、3 需要。
# ---------------------------------------------------------------------------


@dataclass
class _ParsedFile:
    path: Path
    idx: int
    coll: str | None


def _group_by_batch(files: list[Path]) -> dict[tuple[str, str], list[_ParsedFile]]:
    groups: dict[tuple[str, str], list[_ParsedFile]] = {}
    for f in files:
        m = FILE_RE.match(f.name)
        if not m:
            continue  # 無法解析的檔名由契約 1 單獨報告，這裡不重複計入
        date, tm, idx, coll = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        groups.setdefault((date, tm), []).append(_ParsedFile(f, idx, coll))
    return groups


# ---------------------------------------------------------------------------
# 契約 2：無碰撞後綴的批次，序號必為 1..N 連續
# ---------------------------------------------------------------------------


def _check_sequence_contiguous(walk: _Walk) -> ClauseResult:
    total_groups = 0
    bad_groups: list[str] = []
    for dir_path, files in walk.files_by_dir.items():
        for (date, tm), items in sorted(_group_by_batch(files).items()):
            if any(it.coll is not None for it in items):
                continue  # 有碰撞後綴的批次歸契約 3 管，不計入這裡的分母
            total_groups += 1
            idxs = sorted(it.idx for it in items)
            expected = list(range(1, len(items) + 1))
            if idxs != expected:
                bad_groups.append(
                    f"{dir_path.name}/{date}_{tm}（{len(items)} 個檔案）"
                    f"實際序號={idxs}，預期={expected}"
                )
    passed = not bad_groups
    evidence = [f"共檢查 {total_groups} 個無碰撞後綴批次。"]
    if bad_groups:
        evidence.append("序號不連續：" + "；".join(bad_groups))
    elif total_groups == 0:
        evidence.append("（0 個批次代表尚無可驗資料，視為未違反契約）")
    else:
        evidence.append("全數 1..N 連續。")
    return ClauseResult("② 同批序號 1..N 連續", passed, evidence)


# ---------------------------------------------------------------------------
# 契約 3：碰撞後綴（-2、-3…）可辨識。上游慣例是第一份不加後綴、重複才加 -2 起
# （SPEC.md 第 0 節：「目的檔已存在時加 -2、-3 後綴」），故不應出現 -1。
# ---------------------------------------------------------------------------


def _check_collision_suffix(walk: _Walk) -> ClauseResult:
    total_collisions = 0
    examples: list[str] = []
    bad: list[str] = []
    for dir_path, files in walk.files_by_dir.items():
        for (date, tm), items in sorted(_group_by_batch(files).items()):
            for it in items:
                if it.coll is None:
                    continue
                total_collisions += 1
                n = int(it.coll)
                if len(examples) < 5:
                    examples.append(f"{it.path.name}→idx={it.idx},coll={n}")
                if n < 2:
                    bad.append(f"{it.path.name} 的碰撞後綴={n}（上游慣例應 ≥2，不應出現 -1）")
    passed = not bad
    evidence = [f"共發現 {total_collisions} 個碰撞後綴檔案。"]
    if examples:
        evidence.append("範例：" + "、".join(examples))
    if bad:
        evidence.append("異常：" + "；".join(bad))
    elif total_collisions == 0:
        evidence.append("（0 個碰撞後綴檔案代表尚無可驗資料，視為未違反契約）")
    return ClauseResult("③ 碰撞後綴格式（-2、-3…）可辨識", passed, evidence)


# ---------------------------------------------------------------------------
# 契約 4：staging 第一層只能是病患代碼資料夾或 `_unsorted`
# ---------------------------------------------------------------------------


def _check_top_level_layout(walk: _Walk) -> ClauseResult:
    problems: list[str] = []
    if walk.root_loose_files:
        problems.append(
            f"staging 根目錄下有 {len(walk.root_loose_files)} 個落單檔案"
            f"（應放在病患代碼資料夾或 _unsorted 底下）：{_fmt_names(walk.root_loose_files)}"
        )
    for dir_path, subdirs in walk.nested_subdirs.items():
        problems.append(f"{dir_path.name}/ 底下有巢狀子資料夾（應只有一層）：{_fmt_names(subdirs)}")
    for dir_path in walk.top_dirs:
        if not dir_path.name.strip():
            problems.append("發現名稱為空白的第一層資料夾")

    passed = not problems
    names = [d.name for d in walk.top_dirs]
    evidence = [f"第一層資料夾（{len(walk.top_dirs)} 個）：" + ("、".join(names) if names else "（無）")]
    if problems:
        evidence.extend(problems)
    else:
        evidence.append("結構皆符合（無落單根檔案、無二層以上巢狀）。")
    return ClauseResult("④ 第一層為病患代碼或 _unsorted", passed, evidence)


CLAUSE_CHECKS = [
    _check_filenames_parseable,
    _check_sequence_contiguous,
    _check_collision_suffix,
    _check_top_level_layout,
]


# ---------------------------------------------------------------------------
# --config：檢查 ClinicSnap 的 config.json（僅警告，不影響 CONTRACT 判定）。
# ---------------------------------------------------------------------------

REQUIRED_CLINICSNAP_FIELDS = ("token", "saveDir", "archiveMode")
EXPECTED_ARCHIVE_MODE = "by_patient"


def check_clinicsnap_config(path: Path) -> list[str]:
    """讀 ClinicSnap 的 config.json，回傳警告字串列表（空 list＝沒有警告）。

    刻意不 raise：設定檢查是輔助資訊，不是契約條款本身，格式錯誤也只回警告訊息。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"無法讀取 {path}：{exc}"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"{path} 不是合法 JSON：{exc}"]
    if not isinstance(data, dict):
        return [f"{path} 根節點不是 JSON 物件（實際型別：{type(data).__name__}）"]

    warnings: list[str] = []
    for f in REQUIRED_CLINICSNAP_FIELDS:
        if f not in data:
            warnings.append(f"缺少欄位 '{f}'")
    if "archiveMode" in data and data["archiveMode"] != EXPECTED_ARCHIVE_MODE:
        warnings.append(
            f"archiveMode='{data['archiveMode']}'，非本部署前提 '{EXPECTED_ARCHIVE_MODE}'"
            "（見 ../docs/architecture.md 第 3 節；模式不同，staging 資料夾結構也會不同，"
            "契約 ④ 甚至可能整個不適用）"
        )
    return warnings


# ---------------------------------------------------------------------------
# --simulate：自產一組模擬 ClinicSnap 輸出（正常批＋碰撞後綴批＋_unsorted）。
# ---------------------------------------------------------------------------


def build_simulated_staging(root: Path) -> Path:
    """在 root 之下建立 staging/，回傳其路徑。

    只建立空檔——本工具只驗檔名與資料夾結構，不讀影像內容，跟 batching.scan_staging
    本身的行為一致（它也只看檔名和 mtime）。
    """
    staging = root / "staging"
    pid_dir = staging / "A123456789"
    unsorted_dir = staging / "_unsorted"
    pid_dir.mkdir(parents=True, exist_ok=True)
    unsorted_dir.mkdir(parents=True, exist_ok=True)

    # 正常批：同資料夾同時間戳，序號 1..3 連續、無碰撞後綴。
    for idx in (1, 2, 3):
        (pid_dir / f"2026-07-18_143022_{idx}.jpg").touch()

    # 碰撞後綴批：另一個時間戳撞號——兩批同鍵混併時，上游對後到的同名檔補 -2。
    (pid_dir / "2026-07-18_150000_1.jpg").touch()
    (pid_dir / "2026-07-18_150000_2.jpg").touch()
    (pid_dir / "2026-07-18_150000_2-2.jpg").touch()

    # _unsorted：無 ID 批，格式相同。
    (unsorted_dir / "2026-07-19_090000_1.jpg").touch()
    (unsorted_dir / "2026-07-19_090000_2.jpg").touch()

    return staging


# ---------------------------------------------------------------------------
# 主檢查流程與報表輸出
# ---------------------------------------------------------------------------


def run_contract_checks(staging_dir: Path) -> list[ClauseResult]:
    walk = _walk_staging(staging_dir)
    return [check(walk) for check in CLAUSE_CHECKS]


def print_report(
    staging_dir: Path, results: list[ClauseResult], config_warnings: list[str] | None
) -> bool:
    """印出逐條 PASS/FAIL＋證據，結尾印 CONTRACT: PASS|FAIL。回傳整體是否 PASS。"""
    print(f"staging 目錄：{staging_dir}")
    print()
    for i, r in enumerate(results, start=1):
        status = "PASS" if r.passed else "FAIL"
        print(f"[{i}/{len(results)}] {r.label}: {status}")
        for line in r.evidence:
            print(f"      {line}")

    if config_warnings is not None:
        print()
        print("設定檢查（--config，非契約條款，僅供參考，不影響 CONTRACT 判定）：")
        if config_warnings:
            for w in config_warnings:
                print(f"      WARN: {w}")
        else:
            print("      無警告。")

    overall = all(r.passed for r in results)
    print()
    print(f"CONTRACT: {'PASS' if overall else 'FAIL'}")
    return overall


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="contract_test.py",
        description="驗證 ClinicSnap staging 輸出是否符合 clinic_archive.batching 的檔名契約。",
    )
    parser.add_argument(
        "staging_dir",
        nargs="?",
        type=Path,
        default=None,
        help="真實 ClinicSnap staging 資料夾路徑（與 --simulate、--selftest 三選一）",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="自產一組模擬 ClinicSnap 輸出到暫存目錄再驗證（含正常批與碰撞後綴）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="跑 --simulate 並斷言結果為 CONTRACT: PASS（供 CI 使用，不需真實 staging 資料）",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="ClinicSnap 的 config.json 路徑；檢查 token/saveDir/archiveMode 欄位（僅警告）",
    )
    args = parser.parse_args(argv)

    modes_selected = sum([args.staging_dir is not None, args.simulate, args.selftest])
    if modes_selected != 1:
        parser.error("請三選一：<staging_dir> 或 --simulate 或 --selftest。")
    if args.selftest and args.config is not None:
        parser.error("--selftest 不支援搭配 --config（--selftest 內部固定跑自己的設定檢查案例）。")
    return args


def _run_simulate(config_path: Path | None) -> bool:
    with tempfile.TemporaryDirectory(prefix="clinic_archive_contract_") as tmp:
        staging = build_simulated_staging(Path(tmp))
        results = run_contract_checks(staging)
        config_warnings = check_clinicsnap_config(config_path) if config_path else None
        return print_report(staging, results, config_warnings)


def _run_selftest() -> None:
    """跑兩個固定情境並斷言：(a) 乾淨樣本本身要 PASS；(b) 壞掉的 config.json 只印
    WARN、不會拖累 CONTRACT 判定。任何一個斷言失敗都代表 contract_test.py 自己的
    邏輯或 batching.py 的行為跑掉了——這正是這支自我測試存在的目的。
    """
    print("=== --selftest 情境 (a)：--simulate 產生的乾淨樣本必須 PASS ===")
    with tempfile.TemporaryDirectory(prefix="clinic_archive_contract_selftest_") as tmp:
        staging = build_simulated_staging(Path(tmp))
        results = run_contract_checks(staging)
        ok = print_report(staging, results, config_warnings=None)
        assert ok, (
            "selftest 失敗：--simulate 產生的乾淨樣本理應 PASS，但實際 FAIL"
            "（contract_test.py 或 clinic_archive.batching 的行為可能跑掉了）"
        )

        print()
        print("=== --selftest 情境 (b)：壞掉的 ClinicSnap config.json 只應印 WARN ===")
        bad_config = Path(tmp) / "bad_clinicsnap_config.json"
        bad_config.write_text(json.dumps({"token": "x"}), encoding="utf-8")  # 缺 saveDir/archiveMode
        warnings = check_clinicsnap_config(bad_config)
        assert warnings, "selftest 失敗：故意缺欄位的 config.json 應該要產生警告，但沒有"

        results2 = run_contract_checks(staging)
        ok2 = print_report(staging, results2, config_warnings=warnings)
        assert ok2, "selftest 失敗：--config 的警告不該影響 CONTRACT 判定，但整體變成了 FAIL"


def main() -> int:
    args = _parse_args(sys.argv[1:])

    if args.selftest:
        try:
            _run_selftest()
        except AssertionError as exc:
            print(f"SELFTEST FAILED: {exc}")
            print("CONTRACT: FAIL")
            return 1
        return 0

    if args.simulate:
        ok = _run_simulate(args.config)
        return 0 if ok else 1

    staging_dir: Path = args.staging_dir
    if not staging_dir.exists():
        print(f"錯誤：找不到資料夾 {staging_dir}", file=sys.stderr)
        return 2
    if not staging_dir.is_dir():
        print(f"錯誤：{staging_dir} 不是資料夾", file=sys.stderr)
        return 2

    results = run_contract_checks(staging_dir)
    config_warnings = check_clinicsnap_config(args.config) if args.config else None
    ok = print_report(staging_dir, results, config_warnings)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
