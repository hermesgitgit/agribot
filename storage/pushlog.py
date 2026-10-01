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
import datetime
import json
import os
import re
import time

from config import HERMES_OUTBOX_DIR, LAST_PUSH_FILE, TZ_TAIPEI, now_taipei
from logging_setup import logger
from storage.common import STATE_FILE_LOCK, atomic_write_json

_SUMMARY_MAX_CHARS = 600  # 注入 prompt 的摘要長度上限，防止對話 prompt 膨脹
_OUTBOX_TEXT_MAX_CHARS = 3500  # 轉發全文上限（Hermes 端只做彙整，不需要無限長）
_OUTBOX_KEEP_MAX = 50          # 信箱滾動上限：Hermes 長期未消化時刪最舊，防無限堆積


def _write_hermes_outbox(kind: str, text: str):
    """把這則主動推播多寫一份到轉發信箱（一則一檔），供 Hermes 讀取後刪除。
    純附加功能：任何失敗只記 log，絕不影響推播本身與 last_push 記錄。"""
    try:
        os.makedirs(HERMES_OUTBOX_DIR, exist_ok=True)
        safe_kind = re.sub(r"[^\w一-鿿-]", "_", kind)[:40]
        fname = f"{int(time.time() * 1000)}_{safe_kind}.json"
        outpath = os.path.join(HERMES_OUTBOX_DIR, fname)
        atomic_write_json(outpath, {
            "ts": now_taipei().strftime("%Y-%m-%d %H:%M"),
            "kind": kind,
            "text": (text or "")[:_OUTBOX_TEXT_MAX_CHARS],
        })
        # atomic_write_json 走 mkstemp（暫存檔固定 0600）、os.replace 原樣保留權限，
        # 對面讀信箱的 Hermes 容器（uid 1000）會 Errno 13 整批讀不到（2026-08-19
        # 實測：到家報告滿版「無法讀取的項目」）。信箱本來就是要給對面讀的，
        # 這一路放寬到 0644；狀態檔（state/last_push）維持 mkstemp 預設不動。
        os.chmod(outpath, 0o644)
        # 滾動清理：檔名以 epoch ms 開頭，字典序即時間序
        entries = sorted(f for f in os.listdir(HERMES_OUTBOX_DIR) if f.endswith(".json"))
        for old in entries[:-_OUTBOX_KEEP_MAX]:
            os.remove(os.path.join(HERMES_OUTBOX_DIR, old))
    except Exception as e:
        logger.warning(f"⚠️ [Push Log] 寫入 Hermes 轉發信箱失敗（不影響推播）: {e}")


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
    _write_hermes_outbox(kind, text)


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
        # 時效標記：推播裡的感測數值與警戒門檻是「當時」的快照，之後門檻可能已被
        # 手動或 AI 改過、數據也早就變了。實測過 Gemini 會直接照抄這段當作現況回答
        # （連早上的氣溫和舊門檻一起抄），所以明說它只能用來理解使用者在指哪則推播。
        age_text = "時間不明"
        try:
            ts = datetime.datetime.strptime(d.get("ts", ""), "%Y-%m-%d %H:%M").replace(tzinfo=TZ_TAIPEI)
            mins = max(0, int((now_taipei() - ts).total_seconds() // 60))
            age_text = f"{mins} 分鐘前" if mins < 120 else f"約 {mins // 60} 小時前"
        except ValueError:
            pass
        return (
            f"【系統最近一次主動推播（{d.get('ts', '?')}，{age_text}，{d.get('kind', '推播')}）的內容摘要——"
            f"僅供理解使用者提到「剛剛的推播／報告／警報」時是指哪一則。"
            f"注意：這是過去的快照，其中的感測數值與警戒門檻可能已過時；"
            f"回答「現況」一律以本輪即時感測數據與上方【目前農園監控狀態】的門檻為準，不得引用這裡的數字】\n{d.get('summary', '')}"
        )
    except Exception as e:
        logger.warning(f"⚠️ [Push Log] 讀取推播摘要失敗: {e}")
        return ""
