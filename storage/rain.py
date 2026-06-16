# ======================================================================
# 雨量日誌 (Rainfall Log) — 病害「雨水濺潑路徑」的輸入來源
# ======================================================================
# 阿龜微氣候感測器沒有雨量計（其感測項目僅土溫/土濕/EC/氣溫/氣濕），雨量
# 須走 CWA 文山站觀測。哨兵每小時記一筆，供 science.disease 估「近 24h 雨量」。
# 誠實原則：CWA 站離園區數公里、屬鄰近估計；缺資料時回 0（病害模型退化為
# 僅看葉面潮濕路徑，不會誤判）。
import datetime
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from storage.common import STATE_FILE_LOCK


def record_rain(precip_mm) -> None:
    """記錄一筆雨量觀測（mm）。precip_mm 為 None 或非數值時略過、不寫入。"""
    if precip_mm is None:
        return
    try:
        val = float(precip_mm)
    except (ValueError, TypeError):
        return
    ts = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("INSERT OR REPLACE INTO rain_log (ts, precip_mm) VALUES (?, ?)", (ts, val))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning(f"⚠️ 寫入雨量日誌失敗（不影響其餘功能）: {e}")


def recent_rain_mm(hours: int = 24) -> float:
    """
    回傳近 hours 小時內觀測到的最大單筆雨量讀數（mm）。
    用 max 而非加總：CWA「目前雨量」欄位的累計語意不確定（可能為時段雨量或當日
    累積），取最大值對兩種語意都穩健，作為「近期降雨強度/量」的代理。查無資料回 0.0。
    """
    cutoff = (datetime.datetime.now(TZ_TAIPEI) - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            row = conn.execute(
                "SELECT MAX(precip_mm) FROM rain_log WHERE ts >= ?", (cutoff,)).fetchone()
            conn.close()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception as e:
        logger.warning(f"⚠️ 查詢雨量日誌失敗，以 0 處理: {e}")
        return 0.0
