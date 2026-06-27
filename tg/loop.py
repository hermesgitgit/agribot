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
# Telegram Long-Polling 迴圈
# ======================================================================
# offset 持久化落盤防重複處理；訊息逐則「處理完成才推進 offset」（at-least-once，
# 中途 crash/重啟會由 Telegram 重送），handler 以 wait_for 設逾時保險避免卡死整個迴圈。
import asyncio
import json


from config import TELEGRAM_CHAT_ID, TELEGRAM_OFFSET_FILE, TELEGRAM_TOKEN, redact
from logging_setup import logger
from storage.common import atomic_write_json
from tg.api import send_telegram_message
from tg.handlers import handle_message


def _load_telegram_offset() -> int:
    """讀取上次已確認的 Telegram update offset；無檔案或毀損時回 0。"""
    try:
        with open(TELEGRAM_OFFSET_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    except Exception:
        return 0


async def telegram_bot_loop():
    # offset 持久化：只存在記憶體時，程式若在「處理完訊息」與「下一次
    # getUpdates 確認」之間崩潰，重啟後 Telegram 會重發最後一批訊息
    # （造成重複處理、重複回覆）。每批處理完即落盤。
    offset = _load_telegram_offset()
    logger.info("======================================================")
    logger.info("🚀 智慧農務 Agentic Bot 啟動成功！正在監聽 Telegram...")
    logger.info(f"🔒 授權的使用者 Chat ID: {TELEGRAM_CHAT_ID}")
    logger.info("======================================================")
    
    # 啟動時發送上線通知給擁有者
    try:
        await send_telegram_message(int(TELEGRAM_CHAT_ID), "🌱 智慧農務 Agentic Bot 已成功在 NAS 啟動並上線！您可以開始對話了。")
    except Exception as e:
        logger.warning(f"⚠️ 發送啟動通知失敗 (可能 TELEGRAM_CHAT_ID 格式不正確): {e}")
    
    import requests
    loop = asyncio.get_running_loop()
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            
            response = await loop.run_in_executor(
                None, 
                lambda: requests.get(url, params=params, timeout=35).json()
            )
            
            if response.get("ok"):
                for update in response.get("result", []):
                    message = update.get("message")
                    if message and ("text" in message or "photo" in message
                                    or "voice" in message or "audio" in message):
                        # at-least-once：處理完成才推進 offset。若中途 crash／重啟，offset 未推進，
                        # Telegram 會重送此 update（寧可極少數重做，也不靜默漏掉記施肥/收成等訊息）。
                        # wait_for 逾時保險：handler 卡死時放棄該則並照常推進，避免毒訊息卡住整個輪詢。
                        try:
                            await asyncio.wait_for(handle_message(message), timeout=300)
                        except asyncio.TimeoutError:
                            logger.error(f"⚠️ handle_message 逾時(>300s)，跳過 update {update['update_id']}")
                        except Exception as e:
                            logger.error(f"⚠️ handle_message 例外: {redact(e)}")
                    # 不論該則是否需處理，都推進到此 update（已處理／已略過／已放棄）。
                    # 必須同時推進「記憶體中的 offset」——下一次 getUpdates 用的是它，
                    # 只寫檔不更新變數會讓 getUpdates 一直重抓同一批 → 無限迴圈。
                    offset = update["update_id"] + 1
                    try:
                        atomic_write_json(TELEGRAM_OFFSET_FILE, {"offset": offset})
                    except Exception as off_err:
                        logger.warning(f"⚠️ 寫入 Telegram offset 檔失敗: {off_err}")


        except Exception as e:
            logger.warning(f"⚠️ Polling 迴圈發生錯誤，將在 5 秒後重試: {redact(e)}")
            await asyncio.sleep(5)
