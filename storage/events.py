# ======================================================================
# 施肥事件記錄 (Fertilizer Events)
# ======================================================================
import datetime
import json
import os

from config import EVENTS_FILE, TZ_TAIPEI
from logging_setup import logger
from storage.common import STATE_FILE_LOCK, atomic_write_json


def save_fertilizer_event(text):
    """
    記錄一次施肥事件，追加至 events.json（持鎖 + 原子寫入）。
    """
    timestamp = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

    event = {
        "timestamp": timestamp,
        "event_description": text
    }

    try:
        with STATE_FILE_LOCK:
            events = []
            if os.path.exists(EVENTS_FILE):
                try:
                    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                        events = json.load(f)
                except Exception:
                    events = []
            events.append(event)
            events = events[-10:]  # 僅保留最近 10 筆施肥記錄
            atomic_write_json(EVENTS_FILE, events)
        logger.info(f"✅ [Fertilizer Logger] 施肥事件已存檔: {event}")
    except Exception as e:
        logger.error(f"❌ 寫入 events.json 失敗: {e}")


def load_fertilizer_events() -> str:
    """
    讀取施肥記錄，返回易讀文字。
    """
    if not os.path.exists(EVENTS_FILE):
        return "【歷史施肥事件】：目前無施肥記錄。"
    try:
        with STATE_FILE_LOCK:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
        if not events:
            return "【歷史施肥事件】：無施肥記錄。"
        res = "【歷史施肥事件記錄】:\n"
        for ev in events:
            res += f"- {ev['timestamp']}: {ev['event_description']}\n"
        return res
    except Exception as e:
        return f"無法讀取施肥記錄: {e}"
