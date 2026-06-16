# ======================================================================
# 心跳與看門狗 (Heartbeat & Watchdog)
# ======================================================================
# 問題本質：系統死掉時無法自報死訊——「沒收到推播」對人類極不顯眼。
# 三層對策：
# 1. 心跳檔：watchdog 迴圈每分鐘將存活狀態落盤至 /app/data/heartbeat.json。
#    搭配 Docker HEALTHCHECK 可讓容器平台自動偵測假死並重啟（見部署說明）。
# 2. 重啟斷層偵測：啟動時讀取上次心跳，若中斷超過閾值，主動推送
#    「系統曾離線 X 分鐘」通知——把「沒發生的事」轉化為一則看得見的訊息。
# 3. 外部死人開關（選用）：設定 HEARTBEAT_URL 環境變數（如 healthchecks.io
#    的 ping 網址）後，watchdog 每 5 分鐘 ping 一次；NAS 整台死透、連重啟
#    都不會發生時，由外部服務發信通知你。這是唯一能涵蓋「永不復活」情境的機制。
# 另含爬蟲健康監測：連續失敗達閾值即推送 Telegram 警報，恢復時推送復原通知
# ——防止假數據移除後，爬蟲長期失效只默默表現為推播裡的「無資訊」。
import asyncio
import datetime
import json
import os
import threading
import time

import requests

from agent.pending import sweep_expired_pending_events
from config import HEARTBEAT_FILE, HEARTBEAT_URL, TELEGRAM_CHAT_ID, TZ_TAIPEI, redact
from logging_setup import logger
from storage.common import atomic_write_json
from storage.pushlog import record_push
from tg.api import send_telegram_message

SCRAPER_FAILURE_ALERT_THRESHOLD = 3

WATCHDOG_LOCK = threading.Lock()
WATCHDOG_STATE = {
    "agri": {"label": "阿龜微氣候爬蟲", "consecutive_failures": 0, "last_success": None, "alerted": False},
    "cwa": {"label": "氣象署預報爬蟲", "consecutive_failures": 0, "last_success": None, "alerted": False},
    # ET₀ 用的 CWA 開放資料 API 與預報「網頁爬蟲」是兩個獨立元件，必須分開記帳：
    # 共用同一通道時，API 的成功會悄悄歸零爬蟲的連續失敗計數（爬蟲壞了警報永遠
    # 不響），API 金鑰過期也會誤觸發「預報爬蟲失敗」的錯誤警報。
    "cwa_obs": {"label": "CWA 觀測 API (ET₀)", "consecutive_failures": 0, "last_success": None, "alerted": False},
    "pending_alerts": []  # 由爬蟲執行緒寫入、watchdog 迴圈統一發送（Telegram 留在 async 端）
}


def report_scraper_result(name: str, success: bool):
    """
    爬蟲執行緒回報本輪成敗。連續失敗達閾值時排入一則警報；
    警報後首次成功時排入復原通知。本函式只記帳不發訊，發送由 watchdog 迴圈負責。
    """
    now_str = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    with WATCHDOG_LOCK:
        s = WATCHDOG_STATE.get(name)
        if not s:
            return
        if success:
            was_alerted = s["alerted"]
            fail_count = s["consecutive_failures"]
            s["consecutive_failures"] = 0
            s["last_success"] = now_str
            s["alerted"] = False
            if was_alerted:
                WATCHDOG_STATE["pending_alerts"].append(
                    f"✅【系統健康通報】{s['label']}已恢復正常（此前連續失敗 {fail_count} 次）。"
                )
        else:
            s["consecutive_failures"] += 1
            if s["consecutive_failures"] >= SCRAPER_FAILURE_ALERT_THRESHOLD and not s["alerted"]:
                s["alerted"] = True
                WATCHDOG_STATE["pending_alerts"].append(
                    f"🚨【系統健康警報】{s['label']}已連續失敗 {s['consecutive_failures']} 次！\n"
                    f"上次成功時間：{s['last_success'] or '本次啟動後尚無成功記錄'}\n"
                    f"在問題排除前，相關感測/預報數據將持續顯示「無資訊」，"
                    f"GDD 與日彙總可能出現缺數據。建議檢查：\n"
                    f"1. /app/data/logs/agribot.log 中的 ERROR 與 Smart Wait 警告\n"
                    f"2. 阿龜/氣象署網站是否改版或帳密失效\n"
                    f"3. NAS 對外網路是否正常"
                )


def write_heartbeat():
    """將存活狀態落盤（含 epoch 供 Docker HEALTHCHECK 數值比對）。"""
    with WATCHDOG_LOCK:
        hb = {
            "ts": datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": int(time.time()),
            "agri_last_success": WATCHDOG_STATE["agri"]["last_success"],
            "agri_consecutive_failures": WATCHDOG_STATE["agri"]["consecutive_failures"],
            "cwa_last_success": WATCHDOG_STATE["cwa"]["last_success"],
            "cwa_consecutive_failures": WATCHDOG_STATE["cwa"]["consecutive_failures"],
        }
    try:
        atomic_write_json(HEARTBEAT_FILE, hb)
    except Exception as e:
        logger.warning(f"⚠️ [Watchdog] 心跳檔寫入失敗: {e}")


def check_previous_heartbeat(gap_threshold_min=5):
    """
    啟動時讀取上次心跳，回傳 (中斷分鐘數, 最後心跳時間字串)；
    無前次心跳或中斷低於閾值時回傳 (None, None)。
    """
    try:
        if not os.path.exists(HEARTBEAT_FILE):
            return None, None
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            hb = json.load(f)
        gap_min = (time.time() - hb.get("epoch", 0)) / 60.0
        if gap_min >= gap_threshold_min:
            return round(gap_min), hb.get("ts", "未知")
    except Exception as e:
        logger.warning(f"⚠️ [Watchdog] 讀取前次心跳失敗: {e}")
    return None, None


async def watchdog_loop():
    """
    系統自身的守夜人：每 60 秒寫一次心跳檔、發送爬蟲健康警報，
    並（若設定了 HEARTBEAT_URL）每 5 分鐘 ping 一次外部死人開關。
    """
    loop = asyncio.get_event_loop()
    tick = 0
    while True:
        try:
            # 1. 心跳落盤
            write_heartbeat()

            # 2. 發送爬蟲執行緒累積的健康警報（Telegram 發送統一留在 async 端）
            with WATCHDOG_LOCK:
                alerts = WATCHDOG_STATE["pending_alerts"][:]
                WATCHDOG_STATE["pending_alerts"].clear()
            for msg in alerts:
                await send_telegram_message(int(TELEGRAM_CHAT_ID), msg)
                record_push("系統健康通報", msg)

            # 2.5 逾時的待確認收成/施肥事件：視為默認同意，自動落檔並通知
            expired_events = await asyncio.to_thread(sweep_expired_pending_events)
            for ev_chat_id, ev_msg in expired_events:
                await send_telegram_message(ev_chat_id, ev_msg)
                record_push("逾時自動登記通知", ev_msg)

            # 3. 外部死人開關（每 5 分鐘一次）
            if HEARTBEAT_URL and tick % 5 == 0:
                try:
                    await loop.run_in_executor(
                        None, lambda: requests.get(HEARTBEAT_URL, timeout=10))
                except Exception as ping_err:
                    logger.warning(f"⚠️ [Watchdog] 外部心跳 ping 失敗: {redact(ping_err)}")
        except Exception as e:
            logger.warning(f"⚠️ [Watchdog] 看門狗迴圈異常（將於下一輪重試）: {e}")

        tick += 1
        await asyncio.sleep(60)
