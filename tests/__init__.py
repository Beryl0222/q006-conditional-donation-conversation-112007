"""测试包初始化：把项目根目录加入 sys.path，支持 python3 -I 下的 discover。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
