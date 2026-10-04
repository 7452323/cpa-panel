"""测试包：把项目根目录放进 sys.path，便于 `python -m unittest` 直接跑。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
