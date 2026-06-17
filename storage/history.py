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
# 歷史數據永續保存機制 (Semantic History Modeling)
# ======================================================================
import datetime
import json
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from storage.common import STATE_FILE_LOCK


def save_to_history(data_dict):
    """
    將單次抓取的感測器數據加上時間戳記，存入 SQLite 的 sensor_history 表。
    （舊版 JSON 受限於整檔重寫成本只保留 150 筆；改用資料庫後原始數據永久保存，
      成為可回溯的長期檔案庫——prompt 膨脹的防護移至讀取端的 LIMIT。）
    """
    timestamp = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

    record = (
        timestamp,
        data_dict.get("air_temperature", "無資訊"),
        data_dict.get("air_humidity", "無資訊"),
        data_dict.get("soil_temperature", "無資訊"),
        data_dict.get("soil_humidity", "無資訊"),
        data_dict.get("soil_ec", "無資訊")
    )

    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("INSERT OR REPLACE INTO sensor_history VALUES (?, ?, ?, ?, ?, ?)", record)
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM sensor_history").fetchone()[0]
            conn.close()
        logger.info(f"✅ 歷史數據已寫入 SQLite，目前共有 {total} 筆紀錄。")
    except Exception as e:
        logger.error(f"❌ 寫入感測歷史失敗: {e}")


HISTORY_FIELDS = ["timestamp", "air_temperature", "air_humidity", "soil_temperature", "soil_humidity", "soil_ec"]


def query_history_records(limit=40, date_str=None) -> list:
    """
    查詢感測歷史記錄（dict 列表，由舊到新）。
    limit 限定筆數；date_str 給定時改為取該日全部記錄。
    """
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            if date_str:
                rows = conn.execute(
                    "SELECT * FROM sensor_history WHERE timestamp LIKE ? ORDER BY timestamp",
                    (date_str + "%",)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sensor_history ORDER BY timestamp DESC LIMIT ?",
                    (limit,)).fetchall()
                rows.reverse()  # 由舊到新呈現
            conn.close()
        return [dict(zip(HISTORY_FIELDS, r)) for r in rows]
    except Exception as e:
        logger.warning(f"⚠️ 查詢感測歷史失敗: {e}")
        return []


def load_history_summary() -> str:
    """
    從 SQLite 載入最近 40 筆感測歷史，輸出 JSON 字串供 Gemini 進行短期趨勢建模。
    """
    records = query_history_records(limit=40)
    if not records:
        return "目前沒有可用的歷史感測數據。"
    return json.dumps(records, ensure_ascii=False, indent=2)
