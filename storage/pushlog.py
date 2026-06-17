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
# 推播日誌 (Push Log) — 弭平「兩個腦」的記憶落差
# ======================================================================
# 定時推播、GDD 結算、哨兵警報都是「無狀態一次性呼叫」產生的訊息，
# 對話 session 的記憶裡並不存在；但使用者在 Telegram 看到的是同一個聊天窗，
# 會自然地對著推播內容追問（「剛剛說的蟲害是怎麼回事？」）。
# 解法（輕量版）：每次主動推播後記下時間與摘要，對話時注入 prompt，
# 讓對話腦至少知道推播腦最近說過什麼。
import json
import os

from config import LAST_PUSH_FILE, now_taipei
from logging_setup import logger
from storage.common import STATE_FILE_LOCK, atomic_write_json

_SUMMARY_MAX_CHARS = 600  # 注入 prompt 的摘要長度上限，防止對話 prompt 膨脹


def record_push(kind: str, text: str):
    """記錄最近一次系統主動推播（種類＋摘要）。失敗僅記 log，不影響推播本身。"""
    try:
        with STATE_FILE_LOCK:
            atomic_write_json(LAST_PUSH_FILE, {
                "ts": now_taipei().strftime("%Y-%m-%d %H:%M"),
                "kind": kind,
                "summary": (text or "")[:_SUMMARY_MAX_CHARS],
            })
    except Exception as e:
        logger.warning(f"⚠️ [Push Log] 記錄推播摘要失敗: {e}")


def load_last_push_brief() -> str:
    """
    取最近一次主動推播的要點，格式化為可注入對話 prompt 的背景段落；
    尚無記錄時回傳空字串（注入端以空段落自然略過）。
    """
    try:
        if not os.path.exists(LAST_PUSH_FILE):
            return ""
        with STATE_FILE_LOCK:
            with open(LAST_PUSH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        return (
            f"【系統最近一次主動推播（{d.get('ts', '?')}，{d.get('kind', '推播')}）的內容摘要——"
            f"使用者若提到「剛剛的推播／報告／警報」即是指這則】\n{d.get('summary', '')}"
        )
    except Exception as e:
        logger.warning(f"⚠️ [Push Log] 讀取推播摘要失敗: {e}")
        return ""
