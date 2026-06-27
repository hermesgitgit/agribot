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
# Telegram Bot API 發送端與檔案下載
# ======================================================================
import asyncio

import requests

from config import TELEGRAM_TOKEN, redact
from logging_setup import logger

# 下載檔案大小上限（Telegram bot 下載上限約 20MB，照片遠小於此）。防爆記憶體。
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


# ======================================================================
# 耕地狀況快捷按鍵 (Reply Keyboard)
# ======================================================================
# 輸入框下方常駐的快捷按鍵：點一下即等同送出對應文字，免去重複手打。
# 兩顆按鍵服務最高頻的「查耕地狀況」需求：
#   - 耕地快照：走本地 /status，秒回、不爬新數據、零 AI 額度。
#   - 完整分析：等同打「現在耕地的狀況如何？」，交 AI 即時爬取後給完整建議。
# 常數放在發送端模組，供 handlers（攔截按鍵）與 main（啟動掛鍵盤）共用，
# 且 api.py 不反向 import 任何上層模組，無循環依賴之虞。
BTN_SNAPSHOT = "🌱 耕地快照"
BTN_FULL_ANALYSIS = "🔍 完整分析"
FARM_STATUS_QUESTION = "現在耕地的狀況如何？"

FARM_KEYBOARD = {
    "keyboard": [[{"text": BTN_SNAPSHOT}, {"text": BTN_FULL_ANALYSIS}]],
    "resize_keyboard": True,    # 依按鍵數量自動縮成單列高度，不占滿半個螢幕
    "is_persistent": True,      # 常駐顯示（使用者收合後仍可隨時再叫出）
    "input_field_placeholder": "輸入訊息，或點下方按鍵查耕地狀況…",
}


async def send_telegram_message(chat_id, text, reply_markup=None):
    """
    發送 Telegram 訊息（強化版）：
    1. 自動分段：超過 Telegram 4096 字元上限的訊息切塊連發，不再整則發送失敗。
    2. Markdown 渲染：先以 parse_mode=Markdown 發送（AI 回覆與報告中的 **粗體**、
       `等寬` 才能正常顯示而非字面星號）；若內容含不成對符號導致解析失敗
       （HTTP 400），自動退回純文字重送——訊息永不因格式問題而遺失。
    3. reply_markup（選用）：附帶 reply keyboard 等鍵盤定義。分段時只掛在
       最後一段，避免每段都重設鍵盤。
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    loop = asyncio.get_running_loop()
    text = "" if text is None else str(text)
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        try:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
            if reply_markup is not None and is_last:
                payload["reply_markup"] = reply_markup
            r = await loop.run_in_executor(
                None, lambda p=payload: requests.post(url, json=p, timeout=10))
            if r.status_code == 400:
                plain = {"chat_id": chat_id, "text": chunk}
                if reply_markup is not None and is_last:
                    plain["reply_markup"] = reply_markup
                r = await loop.run_in_executor(
                    None, lambda p=plain: requests.post(url, json=p, timeout=10))
            if r.status_code != 200:
                logger.warning(f"⚠️ 發送 Telegram 失敗: {r.status_code}, {redact(r.text)}")
        except Exception as e:
            logger.warning(f"⚠️ 發送 Telegram 網路錯誤: {redact(e)}")


async def send_typing_action(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    payload = {"chat_id": chat_id, "action": "typing"}

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=5))
    except Exception as e:
        logger.warning(f"⚠️ 發送 Typing 動作失敗: {redact(e)}")


async def download_telegram_photo(file_id) -> bytes:
    """
    透過 Telegram Bot API 下載指定 file_id 的圖片檔案內容並回傳 bytes。
    """
    get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    download_url_template = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{{file_path}}"

    loop = asyncio.get_running_loop()
    try:
        # 1. 取得檔案路徑資訊
        res = await loop.run_in_executor(
            None,
            lambda: requests.get(get_file_url, params={"file_id": file_id}, timeout=10)
        )
        if res.status_code != 200:
            logger.warning(f"⚠️ 取得 Telegram 檔案路徑失敗: {res.status_code}, {redact(res.text)}")
            return None

        file_info = res.json()
        if not file_info.get("ok"):
            logger.warning(f"⚠️ Telegram getFile 回傳 ok=False: {file_info}")
            return None

        # 防呆：下載前先用 getFile 回報的大小擋掉過大檔案（避免吃爆記憶體）。
        file_size = file_info["result"].get("file_size") or 0
        if file_size and file_size > MAX_DOWNLOAD_BYTES:
            logger.warning(f"⚠️ 檔案過大（{file_size} bytes > {MAX_DOWNLOAD_BYTES}），拒絕下載。")
            return None

        file_path = file_info["result"]["file_path"]
        download_url = download_url_template.format(file_path=file_path)

        # 2. 下載檔案內容
        file_res = await loop.run_in_executor(
            None,
            lambda: requests.get(download_url, timeout=20)
        )
        if file_res.status_code != 200:
            logger.warning(f"⚠️ 下載 Telegram 圖片失敗: {file_res.status_code}")
            return None

        content = file_res.content
        if len(content) > MAX_DOWNLOAD_BYTES:  # 後備：getFile 沒給 size 時擋下載結果
            logger.warning(f"⚠️ 下載內容過大（{len(content)} bytes），丟棄。")
            return None
        return content
    except Exception as e:
        logger.warning(f"⚠️ 下載 Telegram 檔案時出錯: {redact(e)}")
        return None
