"""應用程式設定：AppConfig dataclass 與載入／初始化邏輯。

行為摘要（見 SPEC.md 第 2 節）：
- `load_config(path)`：path 不存在 → 建立預設設定並寫回；path 存在但 JSON 損毀
  （或內容不是物件）→ 備份為 `.bak` 後以預設值重建，絕不 crash；path 存在且合法
  → 讀入並以已知欄位覆蓋預設值（缺的欄位沿用預設）。
- `db_path` 預設值含 `{data_root}` 佔位符，於 load 時展開為實際路徑字串。
- `ensure_dirs(cfg)`：在 `data_root` 之下建立五個工作子資料夾。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"]

# ensure_dirs() 在 data_root 之下建立的五個工作子資料夾。
WORK_SUBDIRS = ("staging", "inbox", "archive", "review", "trash")


@dataclass
class AppConfig:
    data_root: str = "./clinic_data"
    db_path: str = "{data_root}/clinic.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8770
    settle_seconds: int = 10
    poll_seconds: int = 3
    ocr_version: str = "PPOCRV6"  # PPOCRV6|PPOCRV5
    det_side_len: int = 960
    session_hours: int = 12
    allowed_exts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_EXTS))


def _expand_placeholders(cfg: AppConfig) -> AppConfig:
    """展開欄位值中的 `{data_root}` 佔位符（目前只有 db_path 會用到）。"""
    return dataclasses.replace(cfg, db_path=cfg.db_path.replace("{data_root}", cfg.data_root))


def _write_config(path: Path, cfg: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dataclasses.asdict(cfg), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _backup_corrupt_config(path: Path) -> None:
    backup_path = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, backup_path)
        logger.warning("設定檔損毀，已備份至 %s 並重建預設值", backup_path)
    except OSError:
        logger.exception("備份損毀設定檔失敗：%s", path)


def load_config(path: str | Path) -> AppConfig:
    """載入設定。不存在則建立預設並寫回；損毀則備份 .bak 後以預設值重建（不 crash）。"""
    path = Path(path)

    if not path.exists():
        cfg = AppConfig()
        _write_config(path, cfg)
        return _expand_placeholders(cfg)

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"設定檔根節點必須是 JSON 物件，實際為 {type(data).__name__}")
    except (OSError, ValueError) as exc:
        # 涵蓋 json.JSONDecodeError（ValueError 子類別）與非物件根節點兩種損毀情形。
        logger.warning("讀取設定檔失敗（%s）：%s", exc, path)
        _backup_corrupt_config(path)
        cfg = AppConfig()
        _write_config(path, cfg)
        return _expand_placeholders(cfg)

    known_fields = {f.name for f in dataclasses.fields(AppConfig)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    cfg = AppConfig(**filtered)
    return _expand_placeholders(cfg)


def ensure_dirs(cfg: AppConfig) -> None:
    """在 data_root 之下建立 staging/ inbox/ archive/ review/ trash/ 五個子資料夾。"""
    root = Path(cfg.data_root)
    for name in WORK_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
