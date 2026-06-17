# agribot - 自主農務監控 Telegram bot
# Copyright (C) 2026 Hou-ming Huang
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
