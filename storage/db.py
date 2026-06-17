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
# SQLite 資料庫初始化與舊版 JSON 自動遷移
# ======================================================================
# 主要表：sensor_history（感測史，永久保存）、temp_log（高解析度溫度日誌）、
# daily_summaries（每日 min/max/mean 日報）、rain_log（每小時雨量，供病害雨水
# 濺潑路徑）、harvest_records（割收記錄，由 storage/harvest.py 惰性建表）。
import json
import os
import sqlite3

from config import DAILY_SUMMARY_FILE, DATA_DIR, DB_FILE
from logging_setup import logger
from storage.common import STATE_FILE_LOCK


def init_db():
    """
    初始化 SQLite 資料庫與自動遷移舊的 daily_summary.json 資料。
    任何失敗僅記 ERROR、不向外拋出——本函式在模組載入時執行，
    若因 volume 掛載延遲等原因暫時無法建庫，bot 仍應帶傷啟動
    （日彙總功能降級為空回應，Telegram、爬蟲、GDD 等其餘功能不受牽連）。
    """
    conn = None
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    date TEXT PRIMARY KEY,
                    data TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sensor_history (
                    timestamp TEXT PRIMARY KEY,
                    air_temperature TEXT,
                    air_humidity TEXT,
                    soil_temperature TEXT,
                    soil_humidity TEXT,
                    soil_ec TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS temp_log (
                    ts TEXT PRIMARY KEY,
                    temp REAL
                )
            ''')
            # 雨量日誌：由哨兵每小時自 CWA 觀測記錄一筆，供病害「雨水濺潑路徑」估近 24h 雨量
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rain_log (
                    ts TEXT PRIMARY KEY,
                    precip_mm REAL
                )
            ''')
            conn.commit()

            # --- 自動遷移：daily_summary.json ---
            if os.path.exists(DAILY_SUMMARY_FILE):
                logger.info("📦 [DB Migration] 偵測到舊版 daily_summary.json，開始遷移至 SQLite...")
                try:
                    with open(DAILY_SUMMARY_FILE, "r", encoding="utf-8") as f:
                        summaries = json.load(f)

                    for d_str, d_obj in summaries.items():
                        cursor.execute("INSERT OR IGNORE INTO daily_summaries (date, data) VALUES (?, ?)",
                                       (d_str, json.dumps(d_obj, ensure_ascii=False)))
                    conn.commit()

                    # 遷移完成後更名，避免重複遷移
                    bak_file = DAILY_SUMMARY_FILE + ".bak"
                    os.replace(DAILY_SUMMARY_FILE, bak_file)
                    logger.info(f"✅ [DB Migration] 成功將 {len(summaries)} 筆日彙總遷移至 SQLite，原檔已備份為 {bak_file}")
                except Exception as e:
                    logger.error(f"❌ [DB Migration] 遷移舊日彙總資料失敗: {e}")

            # --- 自動遷移：history.json（原始感測歷史）---
            history_file = os.path.join(DATA_DIR, "history.json")
            if os.path.exists(history_file):
                logger.info("📦 [DB Migration] 偵測到舊版 history.json，開始遷移至 SQLite...")
                try:
                    with open(history_file, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    for r in history:
                        ts = r.get("timestamp")
                        if not ts:
                            continue
                        cursor.execute(
                            "INSERT OR IGNORE INTO sensor_history VALUES (?, ?, ?, ?, ?, ?)",
                            (ts, r.get("air_temperature", "無資訊"), r.get("air_humidity", "無資訊"),
                             r.get("soil_temperature", "無資訊"), r.get("soil_humidity", "無資訊"),
                             r.get("soil_ec", "無資訊")))
                    conn.commit()
                    os.replace(history_file, history_file + ".bak")
                    logger.info(f"✅ [DB Migration] 成功將 {len(history)} 筆感測歷史遷移至 SQLite，原檔已備份為 .bak")
                except Exception as e:
                    logger.error(f"❌ [DB Migration] 遷移舊感測歷史失敗: {e}")

            # --- 自動遷移：temp_log.json（高解析度氣溫日誌）---
            old_temp_log = os.path.join(DATA_DIR, "temp_log.json")
            if os.path.exists(old_temp_log):
                logger.info("📦 [DB Migration] 偵測到舊版 temp_log.json，開始遷移至 SQLite...")
                try:
                    with open(old_temp_log, "r", encoding="utf-8") as f:
                        log = json.load(f)
                    for ts, temp in log.items():
                        cursor.execute("INSERT OR IGNORE INTO temp_log VALUES (?, ?)", (ts, float(temp)))
                    conn.commit()
                    os.replace(old_temp_log, old_temp_log + ".bak")
                    logger.info(f"✅ [DB Migration] 成功將 {len(log)} 個氣溫取樣點遷移至 SQLite，原檔已備份為 .bak")
                except Exception as e:
                    logger.error(f"❌ [DB Migration] 遷移舊氣溫日誌失敗: {e}")

    except Exception as init_err:
        logger.error(f"❌ [DB Init] SQLite 初始化失敗（資料記錄功能將降級，其餘功能不受影響）: {init_err}")
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


# 確保在模組載入時就初始化 DB（與單檔時代的載入時機一致）
init_db()
