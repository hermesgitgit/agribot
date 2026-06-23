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
# 核心主程式入口：四條並行 asyncio 迴圈
# ======================================================================
# 訊息監聽（即時對話）、定時推播（GDD 結算與農務日報）、
# 安全哨兵（每小時門檻巡檢）、看門狗（心跳/健康警報/逾時確認落檔）。
# 啟動時偵測離線斷層並回報、回補缺漏的 GDD 結算。
import asyncio

from config import TELEGRAM_CHAT_ID
from logging_setup import logger
from science.gdd import GDD_BACKFILL_MAX_DAYS
from science.gdd_engine import check_and_update_gdd
from services.push import scheduled_push_loop
from storage.pushlog import record_push
from services.sentinel import hourly_safety_check_loop
from tg.api import FARM_KEYBOARD, send_telegram_message
from tg.loop import telegram_bot_loop
from watchdog import check_previous_heartbeat, watchdog_loop


async def main():
    # 啟動通知與離線斷層偵測：把「沒發生的事」轉化為一則看得見的訊息
    try:
        gap_min, last_ts = check_previous_heartbeat(gap_threshold_min=5)
        if gap_min is not None:
            hours, mins = divmod(gap_min, 60)
            gap_text = f"{hours} 小時 {mins} 分鐘" if hours else f"{mins} 分鐘"
            await send_telegram_message(int(TELEGRAM_CHAT_ID),
                f"⚠️【系統重新上線】偵測到先前心跳中斷約 {gap_text}（最後心跳：{last_ts}）。\n"
                f"離線期間的哨兵巡檢、定時推播與感測記錄可能缺漏；"
                f"缺漏的 GDD 結算與日彙總將於本次啟動自動回補（最多 {GDD_BACKFILL_MAX_DAYS} 天，"
                f"無實測數據的日子會誠實標記跳過）。",
                reply_markup=FARM_KEYBOARD)
            logger.warning(f"⚠️ [Watchdog] 偵測到系統曾離線約 {gap_min} 分鐘（最後心跳: {last_ts}）。")
        else:
            await send_telegram_message(int(TELEGRAM_CHAT_ID),
                "✅ 智慧農務 Bot 已啟動，所有背景迴圈就緒。", reply_markup=FARM_KEYBOARD)
    except Exception as e:
        logger.warning(f"⚠️ [Watchdog] 啟動通知發送失敗: {e}")
    
    # 啟動時先檢查並計算昨日的 GDD
    try:
        logger.info("🌱 [GDD Startup] 偵測到 Bot 啟動，嘗試執行 GDD 每日積溫結算檢查...")
        gdd_msg = await check_and_update_gdd()
        if gdd_msg:
            logger.info("📊 [GDD Startup] 結算成功，正主動發送 GDD 生長積溫日報...")
            await send_telegram_message(int(TELEGRAM_CHAT_ID), gdd_msg)
            record_push("GDD 積溫結算報告", gdd_msg)
    except Exception as e:
        logger.warning(f"⚠️ [GDD Startup] 啟動時計算 GDD 發生異常: {e}")

    # 併行執行 Telegram 監聽 Bot、定時推送服務、背景安全哨兵巡檢與系統看門狗
    await asyncio.gather(
        telegram_bot_loop(),
        scheduled_push_loop(),
        hourly_safety_check_loop(),
        watchdog_loop()
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🔌 程式由使用者手動終止。")
