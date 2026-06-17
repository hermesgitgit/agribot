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
# 長期日彙總存檔 (Daily Summary Archive)
# ======================================================================
# 原始感測記錄不足以支撐「土質蓄水力建模」與「肥料壽命建模」等需要
# 數週尺度的斜率分析。每日結算時將當日各感測值壓縮為一筆 min/max/mean 日報，
# 存入 SQLite 無限期保留，並以精簡格式餵給 Gemini 進行長期趨勢分析
#（資訊密度遠高於原始記錄）。
import json
import re
import sqlite3

from config import DB_FILE
from logging_setup import logger
from storage.common import STATE_FILE_LOCK
from storage.history import query_history_records
from storage.temp_log import get_temps_for_date


def stats_from_strings(str_list):
    """從一串如 '26.5 ℃' 的字串中提取數值並計算 min/max/mean。無有效數值回傳 None。"""
    vals = []
    for s in str_list:
        m = re.search(r'(-?[0-9\.]+)', str(s))
        if m:
            try:
                vals.append(float(m.group(1)))
            except ValueError:
                pass
    if not vals:
        return None
    return {
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "mean": round(sum(vals) / len(vals), 2),
        "n": len(vals)
    }


def save_daily_summary_for_date(date_str) -> bool:
    """
    將指定日期的感測數據彙總為一筆日報存檔。冪等：該日已有日報則直接跳過。
    氣溫優先採用 temp_log 高解析度數據，其餘欄位取自感測歷史。
    """
    with STATE_FILE_LOCK:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # 檢查是否已存在
        cursor.execute("SELECT 1 FROM daily_summaries WHERE date = ?", (date_str,))
        if cursor.fetchone():
            conn.close()
            return False

        # 收集該日的感測歷史記錄（來自 sensor_history 表）
        records = query_history_records(date_str=date_str)

        summary = {}
        # 氣溫優先採用高解析度 temp_log
        log_temps = get_temps_for_date(date_str)
        if log_temps:
            summary["air_temperature"] = {
                "min": round(min(log_temps), 2),
                "max": round(max(log_temps), 2),
                "mean": round(sum(log_temps) / len(log_temps), 2),
                "n": len(log_temps)
            }
        else:
            st = stats_from_strings([r.get("air_temperature", "") for r in records])
            if st:
                summary["air_temperature"] = st

        for field in ["air_humidity", "soil_temperature", "soil_humidity", "soil_ec"]:
            st = stats_from_strings([r.get(field, "") for r in records])
            if st:
                summary[field] = st

        if not summary:
            logger.info(f"ℹ️ [Daily Summary] {date_str} 無任何可彙總的數據，跳過存檔。")
            conn.close()
            return False

        try:
            # INSERT OR IGNORE 與上方預檢語義一致：日報一旦寫入即不可被覆寫（冪等）。
            # 預檢的價值在於提早跳過上面整段彙總計算；rowcount 則作為最終判定。
            cursor.execute("INSERT OR IGNORE INTO daily_summaries (date, data) VALUES (?, ?)",
                           (date_str, json.dumps(summary, ensure_ascii=False)))
            conn.commit()
            res = cursor.rowcount > 0
            if res:
                logger.info(f"✅ [Daily Summary] {date_str} 日彙總已存入 SQLite: {summary}")
            else:
                logger.info(f"ℹ️ [Daily Summary] {date_str} 已有日報，跳過寫入。")
        except Exception as e:
            logger.error(f"❌ [Daily Summary] 寫入 SQLite 失敗: {e}")
            res = False

        conn.close()
        return res


def load_all_daily_summaries() -> dict:
    """讀取全部日彙總（date -> 彙總 dict），供預測自校正引擎以實測驗證預測。"""
    with STATE_FILE_LOCK:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT date, data FROM daily_summaries").fetchall()
        conn.close()
    return {d: json.loads(j) for d, j in rows}


def load_daily_summaries(n=30) -> str:
    """從 SQLite 讀取最近 n 天的日彙總，格式化為精簡文字供 Gemini 進行長期趨勢與斜率分析。"""
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()

            # 若資料表不存在或沒資料，會返回空 list
            cursor.execute("SELECT date, data FROM daily_summaries ORDER BY date DESC LIMIT ?", (n,))
            rows = cursor.fetchall()
            conn.close()

        if not rows:
            return "【長期日彙總趨勢】：尚無累積的日彙總資料（系統將於每日 00:05 自動結算）。"

        # SQLite 取出的是遞減排列 (最新的在前面)，我們把它 reverse 以利呈現趨勢 (由舊到新)
        rows.reverse()

        lines = [f"【長期日彙總趨勢 (最近 {len(rows)} 天，各值格式為 最低~最高 / 平均)】"]
        for date_str, data_json in rows:
            s = json.loads(data_json)
            def fmt(key, unit):
                v = s.get(key)
                if not v:
                    return "—"
                return f"{v['min']}~{v['max']}/{v['mean']}{unit}"
            lines.append(
                f"- {date_str}: 氣溫 {fmt('air_temperature', '℃')}, 空氣濕度 {fmt('air_humidity', '%')}, "
                f"土溫 {fmt('soil_temperature', '℃')}, 土壤濕度 {fmt('soil_humidity', '%')}, EC {fmt('soil_ec', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"無法讀取 SQLite 日彙總資料: {e}"
