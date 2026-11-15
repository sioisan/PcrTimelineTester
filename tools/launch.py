"""PyInstaller 入口：冻结版只负责把图形界面拉起来。"""

from __future__ import annotations

import sys

from PcrTimelineTester.gui import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
