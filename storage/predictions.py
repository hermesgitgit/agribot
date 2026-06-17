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
# 預測日誌持久化 (Prediction Log)
# ======================================================================
# 登記/讀寫 predictions.json；驗證與格式化的純邏輯在 science/calibration.py。
import datetime
import json
import os

from config import PREDICTIONS_FILE, TZ_TAIPEI
from logging_setup import logger
from science.calibration import (
    VALID_PREDICTION_METRICS,
    evaluate_predictions,
    format_prediction_feedback,
)
from storage.common import STATE_FILE_LOCK, atomic_write_json

MAX_PREDICTIONS_KEPT = 50


def save_prediction(metric: str, predicted_value: float, target_date: str, note: str) -> str:
    """登記一筆待驗證預測。回傳結果訊息（成功或拒絕原因）供 AI 知悉。"""
    if metric not in VALID_PREDICTION_METRICS:
        return f"❌ 預測登記失敗：metric 必須是 {VALID_PREDICTION_METRICS} 之一（收到 '{metric}'）。"
    try:
        target = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return f"❌ 預測登記失敗：target_date 格式須為 YYYY-MM-DD（收到 '{target_date}'）。"
    today = datetime.datetime.now(TZ_TAIPEI).date()
    if not (today <= target <= today + datetime.timedelta(days=14)):
        return f"❌ 預測登記失敗：target_date 須介於今天至 14 天內（收到 {target_date}）。"
    try:
        predicted_value = float(predicted_value)
    except (TypeError, ValueError):
        return "❌ 預測登記失敗：predicted_value 必須是數值。"

    entry = {
        "created_at": datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "metric": metric,
        "predicted_value": round(predicted_value, 2),
        "target_date": target_date,
        "note": str(note)[:100],
        "status": "pending"
    }
    try:
        with STATE_FILE_LOCK:
            preds = []
            if os.path.exists(PREDICTIONS_FILE):
                try:
                    with open(PREDICTIONS_FILE, "r", encoding="utf-8") as f:
                        preds = json.load(f)
                except Exception:
                    preds = []
            preds.append(entry)
            preds = preds[-MAX_PREDICTIONS_KEPT:]
            atomic_write_json(PREDICTIONS_FILE, preds)
        logger.info(f"🔮 [Prediction Engine] 已登記預測: {entry}")
        return f"✅ 預測已登記：{target_date} 的 {metric} 預測值 {entry['predicted_value']}（將於該日結算後自動驗證）。"
    except Exception as e:
        return f"❌ 預測登記失敗：{e}"


def evaluate_due_predictions() -> int:
    """
    驗證所有已到期 (target_date 已過) 的 pending 預測：
    以該日「日彙總」的實測平均值為準計算誤差。回傳本次驗證的筆數。
    """
    # 延遲匯入：避免 storage 套件內 predictions ↔ summaries 的載入順序耦合
    from storage.summaries import load_all_daily_summaries

    if not os.path.exists(PREDICTIONS_FILE):
        return 0
    today_str = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    try:
        with STATE_FILE_LOCK:
            with open(PREDICTIONS_FILE, "r", encoding="utf-8") as f:
                preds = json.load(f)
            # 日彙總已遷移至 SQLite（舊 JSON 在遷移後被更名為 .bak 且不再寫入），
            # 驗證來源必須與寫入端一致，否則所有預測都會被誤判為 unverifiable
            summaries = {}
            try:
                summaries = load_all_daily_summaries()
            except Exception as db_err:
                logger.warning(f"⚠️ [Prediction Engine] 讀取 SQLite 日彙總失敗: {db_err}")
            evaluated = evaluate_predictions(preds, summaries, today_str)
            if evaluated:
                atomic_write_json(PREDICTIONS_FILE, preds)
    except Exception as e:
        logger.warning(f"⚠️ [Prediction Engine] 驗證到期預測時出錯: {e}")
        return 0
    if evaluated:
        logger.info(f"🔮 [Prediction Engine] 本次驗證了 {evaluated} 筆到期預測。")
    return evaluated


def load_prediction_feedback(n=8) -> str:
    """讀取預測記錄並格式化校驗回饋，供注入 prompt 形成自我校正迴路。"""
    if not os.path.exists(PREDICTIONS_FILE):
        return "【預測校驗回饋】：目前尚無預測記錄。給出量化判斷時請記得登記預測，以累積你對這塊耕地的校準。"
    try:
        with STATE_FILE_LOCK:
            with open(PREDICTIONS_FILE, "r", encoding="utf-8") as f:
                preds = json.load(f)
        return format_prediction_feedback(preds, n=n)
    except Exception as e:
        return f"【預測校驗回饋】：讀取失敗 ({e})"
