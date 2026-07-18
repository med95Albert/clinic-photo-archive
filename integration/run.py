"""啟動器（Fix-C）：讓 `python run.py` 不論從哪個工作目錄呼叫都能正確 import 套件。

背景：部分環境回報 `pip install -e ".[dev]"` 寫入的 editable-install .pth／finder
沒有確實生效，導致 `python -m clinic_archive.main` 只有在剛好符合某些條件（例如
虛擬環境啟用、且從專案根目錄執行）時才 import 得到 `clinic_archive`；一旦條件不
滿足就是 `ModuleNotFoundError: No module named 'clinic_archive'`，而且現象跟「當下
的工作目錄」綁在一起，很難一眼看出問題在哪。

修法：不依賴 editable install 機制是否生效，直接把「本檔案所在目錄」（也就是
`integration/`，`clinic_archive/` 套件的直接上層）插進 `sys.path` 最前面，再照常
import、呼叫既有的 `clinic_archive.main.main()`。這一招不需要重新 `pip install`，
對任何工作目錄呼叫 `python /任意路徑/integration/run.py` 都有效。

用法（於 `integration/` 目錄下）：

    python run.py
    python run.py --config /path/to/config.json

引數會原封不動交給 `clinic_archive.main.main()`（同一套 argparse 定義，見
`clinic_archive/main.py`）。

注意：`run.py` 只解決「能不能 import 到套件」的問題，不改變 config.json／
data_root 的路徑語意——兩者仍是相對路徑，實際落點由**執行當下的工作目錄**決定，
不是本檔案所在位置。也就是說，就算你在別的目錄下執行
`python /path/to/integration/run.py`，`config.json` 與 `clinic_data/` 還是會建立在
你當下所在的工作目錄，不會自動跟著 run.py 的位置走。固定部署建議永遠從同一個
目錄啟動（例如寫進開機腳本時先 `cd` 進 `integration/` 再執行），或在 config.json
內把 `data_root` 改成絕對路徑；細節見 README「資料夾佈局」一節。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 務必在 import clinic_archive 之前插入，且插在最前面（index 0）以優先於其他同名套件。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clinic_archive.main import main  # noqa: E402  (需先調整 sys.path 才能 import)

if __name__ == "__main__":
    main()
