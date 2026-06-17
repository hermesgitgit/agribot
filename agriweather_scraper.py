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
# 過渡門面 (Transitional Façade)
# ======================================================================
# 本檔原為 3600 餘行的單檔程式，現已按層拆分為下列模組；
# 為維持既有部署（python agriweather_scraper.py）與任何
# `import agriweather_scraper` 的舊介面不變，本檔保留為純 re-export 門面。
# 新程式請直接 import 對應模組；門面穩定運行一段時間後可改用 main.py 入口。
#
#   config.py          環境變數、金鑰遮罩、路徑與測站常數、台北時區
#   logging_setup.py   stdout + 輪替檔案雙通道日誌
#   science/           科學運算層（純計算核心 + GDD 結算編排）
#     ├─ gdd.py            作物資料庫、上下限修正式 GDD 公式（純）
#     ├─ gdd_engine.py     逐日結算與多日回補（I/O 編排）
#     ├─ et0.py            FAO-56 Penman-Monteith（純）
#     ├─ cadence.py        割收節律歸納（純）
#     └─ calibration.py    預測自校正：誤差驗證與回饋格式化（純）
#   storage/           持久化層（state 交易、SQLite 四表、事件/預測/照片）
#   scrapers/          資料擷取層（阿龜 Playwright、CWA 預報爬蟲與觀測 API）
#   agent/             AI Agent 層（Gemini session、14 工具含知識庫檢索、Guard、待確認事件）
#   tg/                Telegram 介面層（發送端、訊息處理、long-polling）
#   services/          定時推播與安全哨兵
#   watchdog.py        心跳、爬蟲健康記帳、看門狗迴圈
#   main.py            四條並行 asyncio 迴圈的進入點
#
# 全局設計哲學（拆分後依然成立，修改任何模組前請先讀）：
# 1. 誠實原則——爬取失敗回報「無資訊」而非假資料、缺數據日不估算、
#    ET₀ 標注成色、節律樣本不足明說。
# 2. 自我守望——系統假設自己會死掉（心跳、斷層偵測、外部開關）、
#    假設爬蟲會壞（連續失敗警報）、假設 AI 會犯錯（限幅、確認制、預測校驗）。
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

# --- 原單檔時代的頂層 import（保留以維持模組屬性相容） ---
import asyncio
import contextvars
import datetime
import glob
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import requests
from google import genai
from PIL import Image
from playwright.sync_api import sync_playwright

# --- 基礎設施 ---
from logging_setup import LOG_DIR, logger, _setup_logger
from config import (
    GEMINI_API_KEY, AGRI_USERNAME, AGRI_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    CWA_API_KEY, CWA_STATION_ID, CWA_STATION_NAME, CWA_STATION_LAT, CWA_STATION_LON,
    CWA_OBS_API, DAILY_SUMMARY_FILE, DB_FILE, HEARTBEAT_FILE, HEARTBEAT_URL,
    PREDICTIONS_FILE, TELEGRAM_OFFSET_FILE, redact,
)

# --- 持久化層 ---
from storage.common import STATE_FILE_LOCK, atomic_write_json
from storage.state import load_state, save_state, update_state
from storage.db import init_db  # import 時即建表並遷移舊版 JSON（與單檔時代一致）
from storage.temp_log import (
    TEMP_LOG_RETENTION_DAYS, extract_time_temp_series, merge_temp_log,
    get_temps_for_date, iso_to_taipei_str as _iso_to_taipei_str,
)
from storage.history import (
    HISTORY_FIELDS as _HISTORY_FIELDS, save_to_history,
    query_history_records, load_history_summary,
)
from storage.summaries import (
    stats_from_strings as _stats_from_strings,
    save_daily_summary_for_date, load_daily_summaries,
)
from storage.predictions import (
    MAX_PREDICTIONS_KEPT, save_prediction,
    evaluate_due_predictions, load_prediction_feedback,
)
from storage.events import save_fertilizer_event, load_fertilizer_events
from storage.photos import save_photo, get_past_photo, get_photo_staleness_days
from storage.harvest import (
    HARVEST_TABLE_READY, record_harvest, get_harvest_cadence_summary,
    _ensure_harvest_table, _current_accumulated_gdd, _collect_regen_intervals,
)

# --- 科學運算層 ---
from science.gdd import (
    CROP_GDD_DATABASE, GDD_BACKFILL_MAX_DAYS,
    gdd_from_minmax, lookup_crop_info, match_crop_key,
)
from science.gdd_engine import calculate_daily_gdd, check_and_update_gdd
from science.et0 import (
    calculate_et0,
    safe_float as _safe_float,
    _saturation_vapour_pressure,
)
from science.cadence import HARVEST_MIN_SAMPLES, HARVEST_RECENT_WINDOW, mean as _mean, cadence_brief
from science.calibration import VALID_PREDICTION_METRICS, METRIC_LABELS

# --- 資料擷取層 ---
from scrapers.agri import format_6h_history, get_agriweather_data
from scrapers.cwa import (
    CWA_SCRAPER_LOCK, fetch_cwa_observation, get_cwa_weather_forecast, get_et0_report,
)
from scrapers.waits import smart_wait, wait_for_intercept

# --- 看門狗 ---
from watchdog import (
    SCRAPER_FAILURE_ALERT_THRESHOLD, WATCHDOG_LOCK, WATCHDOG_STATE,
    check_previous_heartbeat, report_scraper_result, watchdog_loop, write_heartbeat,
)

# --- AI Agent 層 ---
from agent.pending import (
    PENDING_EVENTS, PENDING_EVENT_TTL, _current_chat_ctx,
    classify_confirmation, clear_pending_event, commit_pending_event,
    get_pending_event, set_current_chat_context, sweep_expired_pending_events,
    _set_pending_event, _DENY_WORDS, _AFFIRM_EXACT, _AFFIRM_PHRASES,
)
from agent.guard import (
    MAX_THRESHOLD_STEP, URL_PATTERN,
    apply_crop_command, apply_threshold_command, strip_links,
)
from agent.prompts import SYSTEM_INSTRUCTION, build_state_summary, get_current_time_context
from agent.tools import (
    AGENT_TOOLS,
    tool_get_realtime_sensor_data, tool_get_weather_forecast, tool_query_recent_history,
    tool_query_daily_summaries, tool_get_garden_status, tool_set_thresholds, tool_set_crop,
    tool_record_prediction, tool_query_prediction_history, tool_query_harvest_cadence,
    tool_get_et0_evapotranspiration, tool_record_harvest_event, tool_record_fertilizer_event,
)
from agent.session import (
    build_agent_model, chat_locks, chat_sessions, generate_oneshot_no_tools,
    generate_oneshot_with_retry, get_chat_lock, get_or_create_chat, reset_chat,
    send_message_with_retry, start_new_gemini_chat, _today_taipei_str,
)

# --- Telegram 介面層 / 服務層 / 主程式 ---
from tg.api import download_telegram_photo, send_telegram_message, send_typing_action
from tg.handlers import handle_local_command, handle_message
from tg.loop import telegram_bot_loop, _load_telegram_offset
from services.push import scheduled_push_loop, trigger_scheduled_push
from services.sentinel import hourly_safety_check_loop
from main import main


def _harvest_cadence_brief(crop_name) -> str:
    """舊版私有介面的相容墊片：節律摘要改由 science.cadence.cadence_brief 提供。"""
    days, gdds = _collect_regen_intervals(crop_name)
    return cadence_brief(days, gdds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🔌 程式由使用者手動終止。")
