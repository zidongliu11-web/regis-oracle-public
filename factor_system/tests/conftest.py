"""把项目根目录加入 sys.path，保证 `python -m pytest factor_system/tests` 可导入 factor_system / quantqc。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
