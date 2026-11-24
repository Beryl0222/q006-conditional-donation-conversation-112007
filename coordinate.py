#!/usr/bin/env python3
"""协调员命令行入口：python3 -I coordinate.py data/records.json pickup

-I 隔离模式下解释器不自动补脚本目录，这里显式加入，避免资料目录里
被植入同名模块（如 json.py）影响导入。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from donation.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
