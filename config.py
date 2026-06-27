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
# 設定中心 (Config)
# ======================================================================
# 集中管理：環境變數（API 金鑰與敏感資訊）、資料檔路徑、CWA 測站參數、
# 台北時區等跨模組共用常數。零內部依賴（只允許 import logging_setup），
# 是整個依賴圖的最底層葉子。
import datetime
import os
import sys

from logging_setup import logger

# ----------------------------------------------------------------------
# API 金鑰與敏感資訊自動載入區 (雲端環境變數版)
# ----------------------------------------------------------------------
AGRI_API_KEY = os.getenv("AGRI_API_KEY")
AGRI_SUID = os.getenv("AGRI_SUID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")  # 報告備援（Gemini 壅塞時改打 NVIDIA），選填
AGRI_USERNAME = os.getenv("AGRI_USERNAME")
AGRI_PASSWORD = os.getenv("AGRI_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([GEMINI_API_KEY, AGRI_API_KEY, AGRI_SUID, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
    logger.error("❌ 錯誤：有環境變數未設定！分享檔案時，請確保啟動環境已帶入以下全數變數：")
    logger.error("GEMINI_API_KEY, AGRI_API_KEY, AGRI_SUID, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID")
    sys.exit(1)

# 外部死人開關（選用，如 healthchecks.io 的 ping 網址），未設定則跳過外部 ping
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL")

# ----------------------------------------------------------------------
# CWA 氣象開放資料 API 設定（觀測資料 O-A0001-001）
# ----------------------------------------------------------------------
# 用於取得博嘉國小（文山站）的即時實測氣象，作為 ET₀ 虛擬感測器的輸入。
# 授權碼一律由環境變數帶入，不寫死於程式（換碼免改碼、分享不外洩）。
CWA_API_KEY = os.getenv("CWA_API_KEY", "").strip()
CWA_STATION_ID = "C0AC80"        # 文山站（博嘉國小，海拔約 40m，木柵路四段）
CWA_STATION_NAME = "文山(博嘉國小)"
CWA_STATION_LAT = 25.0024        # 緯度（ET₀ 輻射計算需要）
CWA_STATION_LON = 121.5757
CWA_OBS_API = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001"

# ----------------------------------------------------------------------
# 資料檔路徑（NAS volume 掛載於 /app/data）
# ----------------------------------------------------------------------
DATA_DIR = "/app/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
DB_FILE = os.path.join(DATA_DIR, "agriweather.db")
DAILY_SUMMARY_FILE = os.path.join(DATA_DIR, "daily_summary.json")  # 舊版 JSON，僅供啟動遷移
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.json")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
TELEGRAM_OFFSET_FILE = os.path.join(DATA_DIR, "telegram_offset.json")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
KNOWLEDGE_DB_FILE = os.path.join(DATA_DIR, "knowledge.db")  # 農業部出版品知識庫（離線建置後放入）
LAST_PUSH_FILE = os.path.join(DATA_DIR, "last_push.json")   # 最近一次主動推播摘要（供對話腦知悉推播腦說過什麼）

# 全系統一律以台北時區運作，不受容器內部 OS 時區偏差影響
TZ_TAIPEI = datetime.timezone(datetime.timedelta(hours=8))


def now_taipei() -> datetime.datetime:
    """目前的台北時間（aware datetime）。"""
    return datetime.datetime.now(TZ_TAIPEI)


def redact(text) -> str:
    """
    遮罩字串中的敏感金鑰（Telegram token、API 金鑰、密碼、心跳網址）。
    requests/urllib 的例外訊息常內嵌完整請求 URL，而 Telegram bot token
    就嵌在 URL 路徑中（/bot<TOKEN>/...）——凡是「把例外原文寫進日誌
    或發給使用者」的地方，都必須先經過本函式，防止秘密落盤至
    /app/data/logs/ 長期留存。
    """
    s = str(text)
    for secret in (TELEGRAM_TOKEN, GEMINI_API_KEY, NVIDIA_API_KEY, AGRI_PASSWORD, AGRI_API_KEY, CWA_API_KEY, HEARTBEAT_URL):
        if secret:
            s = s.replace(secret, "***")
    return s
