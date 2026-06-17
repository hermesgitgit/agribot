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
# 高解析度氣溫日誌 (Temp Log) — GDD 的主要數據源
# ======================================================================
# 每次爬取阿龜時，攔截到的「區間資料 API」回應（6 小時 / 24 小時）會被合併進
# SQLite 的 temp_log 表（時間戳主鍵天然去重，保留最近數天）。
# 相比事件式稀疏取樣（凌晨低溫與午後高峰常落在取樣間隙），
# 這份日誌能精確涵蓋每日真正的 T_max / T_min，大幅提升 GDD 結算精度。
import datetime
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from storage.common import STATE_FILE_LOCK

TEMP_LOG_RETENTION_DAYS = 4


def iso_to_taipei_str(iso_str):
    """將阿龜 API 的 ISO (UTC) 時間字串轉為台北時間 'YYYY-MM-DD HH:MM'。解析失敗回傳 None。"""
    try:
        s = str(iso_str).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def extract_time_temp_series(api_json):
    """從阿龜區間 API 回應中提取 (時間序列, 氣溫序列)。格式不符時回傳 ([], [])。"""
    try:
        device_key = None
        for key in api_json.keys():
            if key not in ["status", "type"]:
                device_key = key
                break
        if not device_key:
            return [], []
        dev = api_json[device_key]
        return (dev.get("time", []) or []), (dev.get("air_temperature", []) or [])
    except Exception:
        return [], []


def merge_temp_log(api_json) -> int:
    """
    將一份區間 API 回應中的氣溫取樣點合併進 SQLite 的 temp_log 表
    （以時間戳為主鍵天然去重、自動修剪過期條目）。回傳新增的點數。
    """
    times, temps = extract_time_temp_series(api_json)
    if not times:
        return 0
    cutoff = (datetime.datetime.now(TZ_TAIPEI) - datetime.timedelta(days=TEMP_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M")
    added = 0
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            for i, t in enumerate(times):
                if i >= len(temps) or temps[i] is None:
                    continue
                key = iso_to_taipei_str(t)
                if not key:
                    continue
                try:
                    cursor.execute("INSERT OR IGNORE INTO temp_log VALUES (?, ?)", (key, float(temps[i])))
                    added += cursor.rowcount
                except (TypeError, ValueError):
                    continue
            # 字典序比較對 "YYYY-MM-DD HH:MM" 格式即為時間序比較
            cursor.execute("DELETE FROM temp_log WHERE ts < ?", (cutoff,))
            conn.commit()
            total = cursor.execute("SELECT COUNT(*) FROM temp_log").fetchone()[0]
            conn.close()
        if added:
            logger.info(f"🌡️ [Temp Log] 已合併 {added} 個新氣溫取樣點（現存 {total} 點）。")
    except Exception as e:
        logger.warning(f"⚠️ [Temp Log] 合併氣溫日誌失敗: {e}")
    return added


def get_temps_for_date(date_str) -> list:
    """從 SQLite temp_log 表取出指定日期（台北時間）的所有氣溫值。"""
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute("SELECT temp FROM temp_log WHERE ts LIKE ?", (date_str + "%",)).fetchall()
            conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning(f"⚠️ [Temp Log] 讀取氣溫日誌失敗: {e}")
        return []
