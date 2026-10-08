import sys
from pathlib import Path

# 讓 `pytest` 直接從任何位置執行都找得到 core/ api/ strategies/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _preopen_filter_off_by_default(monkeypatch):
    """盤前試算行情的隔離取決於「報價時間是幾點」。既有測試常用 time.time() 當報價時間，
    不能因為剛好在 08:30~08:45 或 14:50~15:00 執行就失敗；要測隔離的測試自己打開 qh.FILTER_PREOPEN。"""
    import core.quote_hub as qh
    monkeypatch.setattr(qh, "FILTER_PREOPEN", False)
