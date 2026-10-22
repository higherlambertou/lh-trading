import sys
from pathlib import Path

# 讓 `pytest` 直接從任何位置執行都找得到 core/ api/ strategies/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _never_touch_the_real_backend(monkeypatch):
    """登入券商前的連線數檢查（core/broker_guard.py）預設會去問「正在跑的正式盤後端」。測試絕不能碰真實後端
    （結果會隨當下連線數變、還會把真實 .env 載進測試進程）——指到一個必定連不上的位址，等於「後端沒開」。"""
    monkeypatch.setenv("USAGE_URL", "http://127.0.0.1:9/api/position/usage")


@pytest.fixture(autouse=True)
def _no_real_pushes_or_state_files(monkeypatch, tmp_path):
    """推播、持倉狀態檔、手動停損落地（update.md 發現 32）：測試預設全部關閉——絕不送真的推播、不寫進 repo 的 data/。
    要測的測試自己指定暫存路徑與假通道。"""
    for k in ("NOTIFY_TELEGRAM_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID", "NOTIFY_NTFY_URL", "NOTIFY_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOTIFY_COOLDOWN_FILE", "")            # 空字串＝冷卻只用記憶體
    monkeypatch.setenv("NOTIFY_SKIP_DOTENV", "1")             # CLI 不讀真的 .env（裡面有真的 Telegram token）
    monkeypatch.delenv("SIMULATION", raising=False)
    monkeypatch.setenv("POSITION_STATE_FILE", "false")
    monkeypatch.setenv("MANUAL_WATCH_PERSIST", "false")
    import core.notify as nt
    nt._memory_last.clear()

    class NoRealNetwork(BaseException):                        # BaseException：不會被 `except Exception` 吞掉，測試會直接失敗
        pass

    def _never(*a, **k):
        raise NoRealNetwork("測試不能真的送出推播或連 Telegram：請用假的 _http_post／_http_get（monkeypatch.setattr(nt, '_http_post', fake)）")

    monkeypatch.setattr(nt, "REAL_HTTP_POST", nt._http_post, raising=False)       # 要測「真的函式」的測試（只驗網址 scheme，不連網）用這兩個
    monkeypatch.setattr(nt, "REAL_HTTP_GET", nt._http_get, raising=False)
    monkeypatch.setattr(nt, "_http_post", _never)
    monkeypatch.setattr(nt, "_http_get", _never)
    import core.premarket_log as pml                           # 盤前快照的預設資料庫（真的 data/trade_log.db）換成暫存的：沒傳 store 的呼叫也不能寫到真的紀錄
    monkeypatch.setattr(pml, "_default_store", pml.PremarketStore(tmp_path / "default_premarket.db"))
    monkeypatch.setattr(pml, "_intel_path", lambda: tmp_path / "no_intel.db")       # 盤前快照的 lh-intel 來源：測試不能讀真的 intel.db（不存在＝這個來源失敗、改用下一個）


@pytest.fixture(autouse=True)
def _preopen_filter_off_by_default(monkeypatch):
    """盤前試算行情的隔離取決於「報價時間是幾點」。既有測試常用 time.time() 當報價時間，
    不能因為剛好在 08:30~08:45 或 14:50~15:00 執行就失敗；要測隔離的測試自己打開 qh.FILTER_PREOPEN。"""
    import core.quote_hub as qh
    monkeypatch.setattr(qh, "FILTER_PREOPEN", False)


@pytest.fixture(autouse=True)
def _fresh_intraday_context(monkeypatch):
    """盤中時段脈絡（core/intraday_context.py）是模組層級的單例，會把算好的資料與背景工作留到下一個測試；每個測試換一個新的。"""
    import core.daily_summary as ds
    import core.intraday_context as ic
    monkeypatch.setattr(ic, "intraday_context", ic.IntradayContext())
    monkeypatch.setattr(ds, "intraday_context", ic.intraday_context)
