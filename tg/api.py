# ======================================================================
# Telegram Bot API 發送端與檔案下載
# ======================================================================
import asyncio

import requests

from config import TELEGRAM_TOKEN, redact
from logging_setup import logger


async def send_telegram_message(chat_id, text):
    """
    發送 Telegram 訊息（強化版）：
    1. 自動分段：超過 Telegram 4096 字元上限的訊息切塊連發，不再整則發送失敗。
    2. Markdown 渲染：先以 parse_mode=Markdown 發送（AI 回覆與報告中的 **粗體**、
       `等寬` 才能正常顯示而非字面星號）；若內容含不成對符號導致解析失敗
       （HTTP 400），自動退回純文字重送——訊息永不因格式問題而遺失。
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    loop = asyncio.get_running_loop()
    text = "" if text is None else str(text)
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for chunk in chunks:
        try:
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
            r = await loop.run_in_executor(
                None, lambda p=payload: requests.post(url, json=p, timeout=10))
            if r.status_code == 400:
                plain = {"chat_id": chat_id, "text": chunk}
                r = await loop.run_in_executor(
                    None, lambda p=plain: requests.post(url, json=p, timeout=10))
            if r.status_code != 200:
                logger.warning(f"⚠️ 發送 Telegram 失敗: {r.status_code}, {r.text}")
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
            logger.warning(f"⚠️ 取得 Telegram 檔案路徑失敗: {res.status_code}, {res.text}")
            return None

        file_info = res.json()
        if not file_info.get("ok"):
            logger.warning(f"⚠️ Telegram getFile 回傳 ok=False: {file_info}")
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

        return file_res.content
    except Exception as e:
        logger.warning(f"⚠️ 下載 Telegram 檔案時出錯: {redact(e)}")
        return None
