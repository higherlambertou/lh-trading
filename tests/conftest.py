import sys
from pathlib import Path

# 讓 `pytest` 直接從任何位置執行都找得到 core/ api/ strategies/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
