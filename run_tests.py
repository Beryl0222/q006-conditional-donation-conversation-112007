#!/usr/bin/env python3
"""测试入口：python3 -I run_tests.py

显式把项目根加入 sys.path，使隔离模式（-I）下也能发现 donation 包与
tests 包，避免依赖 cwd 自动入路径或环境变量。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
