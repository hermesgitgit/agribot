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
import contextvars

import requests

from config import TELEGRAM_TOKEN, redact
from logging_setup import logger

# ----------------------------------------------------------------------
# 橋接擷取模式（供 bridge/server.py 使用）
# ----------------------------------------------------------------------
# send_telegram_message 是全系統唯一的發送出口（handlers/push/sentinel 等一律
# 經此函式），故只需在此單點攔截即可讓 Hermes 橋接「借用」既有的對話/指令邏輯、
# 取回文字答案而不觸碰真正的 Telegram 頻道。ContextVar 是 asyncio-task-local，
# 一般 Telegram 輪詢路徑（未呼叫 start_capture）行為完全不受影響、零改動風險。
_capture_var: "contextvars.ContextVar[list | None]" = contextvars.ContextVar("capture", default=None)


def start_capture():
    """開始擷取本 asyncio 任務接下來呼叫的 send_telegram_message 內容，回傳還原用的 token。"""
    return _capture_var.set([])


def stop_capture(token) -> str:
    """停止擷取並還原，回傳擷取期間累積的訊息文字（多則以換行連接）。"""
    chunks = _capture_var.get() or []
    _capture_var.reset(token)
    return "\n".join(c for c in chunks if c)


def is_capturing() -> bool:
    """目前這個 asyncio 任務是否處於橋接擷取模式（回覆給 HTTP 呼叫端而非擁有者）。

    擷取模式下擁有者看不到任何訊息，因此所有「先告知、給喊停視窗」的機制都不成立，
    需要據此拒絕會寫入長期記錄的操作。
    """
    return _capture_var.get() is not None

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
BTN_FERTILIZED = "🧪 已施肥"          # 一鍵登記施肥（等同對 AI 說「我施肥了」，走既有確認流程）
FARM_STATUS_QUESTION = "現在耕地的狀況如何？"
FERTILIZE_PHRASE = "我施肥了"          # 按下「已施肥」時代打的句子，交由 AI 走施肥登記流程

FARM_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_SNAPSHOT}, {"text": BTN_FULL_ANALYSIS}],  # 第一列：查耕地狀況
        [{"text": BTN_FERTILIZED}],                            # 第二列：登記類動作
    ],
    "resize_keyboard": True,    # 依按鍵數量自動縮成單列高度，不占滿半個螢幕
    "is_persistent": True,      # 常駐顯示（使用者收合後仍可隨時再叫出）
    "input_field_placeholder": "輸入訊息，或點下方按鍵查狀況／記施肥…",
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
    text = "" if text is None else str(text)

    capture = _capture_var.get()
    if capture is not None:
        # 橋接擷取模式：收進清單、不發真正的 Telegram API（避免與 Hermes 的轉述重複通知）。
        capture.append(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    loop = asyncio.get_running_loop()
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        # 多段訊息之間插一個間隔：Telegram 對同一對話約 1 則/秒，零間隔連發會讓
        # 後段吃 429。長知識庫回答過去就是這樣在句子中間斷掉的。
        if idx > 0:
            await asyncio.sleep(1.1)
        # 只在 400 退回純文字、其餘失敗直接丟棄，等於哨兵警報遇到一次 5xx 或
        # 連線逾時就永久消失。改成退避重試：429 照 retry_after 走，5xx／網路錯誤
        # 指數退避，四次都失敗才放棄並留下 error 等級日誌。
        sent = False
        for attempt in range(4):
            try:
                payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
                if reply_markup is not None and is_last:
                    payload["reply_markup"] = reply_markup
                r = await loop.run_in_executor(
                    None, lambda p=payload: requests.post(url, json=p, timeout=10))
                if r.status_code == 400:
                    # Markdown 解析失敗 → 退成純文字（這條是「換內容」不是「重試」）
                    plain = {"chat_id": chat_id, "text": chunk}
                    if reply_markup is not None and is_last:
                        plain["reply_markup"] = reply_markup
                    r = await loop.run_in_executor(
                        None, lambda p=plain: requests.post(url, json=p, timeout=10))
                if r.status_code == 200:
                    sent = True
                    break
                if r.status_code == 429:
                    try:
                        wait = int((r.json().get("parameters") or {}).get("retry_after", 1))
                    except Exception:
                        wait = 1
                    logger.warning(f"⚠️ Telegram 限流，{wait} 秒後重送第 {idx + 1} 段")
                    await asyncio.sleep(min(wait, 30))
                    continue
                if 500 <= r.status_code < 600:
                    logger.warning(f"⚠️ Telegram {r.status_code}，重試第 {idx + 1} 段")
                    await asyncio.sleep(2 ** attempt)
                    continue
                # 其餘 4xx 重送也不會好（例如被封鎖、chat 不存在）
                logger.error(f"❌ 發送 Telegram 失敗: {r.status_code}, {redact(r.text)}")
                break
            except Exception as e:
                logger.warning(f"⚠️ 發送 Telegram 網路錯誤（第 {attempt + 1} 次）: {redact(e)}")
                await asyncio.sleep(2 ** attempt)
        if not sent:
            logger.error(f"❌ 第 {idx + 1}/{len(chunks)} 段訊息最終未送達，內容前 60 字："
                         f"{redact(chunk[:60])}")


async def send_typing_action(chat_id):
    if _capture_var.get() is not None:
        # 橋接擷取模式：不對真正的 Telegram 頻道送 typing——否則使用者會看到
        # agribot「輸入中…」卻永遠等不到訊息（答案是回給 Hermes 的）。
        return
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
