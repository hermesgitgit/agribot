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
# 預測自校正 — 純邏輯核心 (Science: Prediction Self-Calibration)
# ======================================================================
# Agentic 核心迴路之一：AI 給建議時順帶登記「可驗證的量化預測」
# （例如：明日土壤濕度均值約 35%）。每日結算時，系統自動以日彙總實測值
# 驗證到期預測並計算誤差，將命中/偏離回饋進後續 prompt——
# 模型不再每次從零開始，而是持續被自己的預測誤差校正。
# 本模組為純函數：預測列表與日彙總由呼叫端傳入（檔案/DB I/O 在 storage/predictions.py）。

VALID_PREDICTION_METRICS = ["air_temperature", "air_humidity", "soil_temperature", "soil_humidity", "soil_ec"]

METRIC_LABELS = {
    "air_temperature": "氣溫", "air_humidity": "空氣濕度",
    "soil_temperature": "土溫", "soil_humidity": "土壤濕度", "soil_ec": "土壤EC"
}


def evaluate_predictions(preds: list, summaries: dict, today_str: str) -> int:
    """
    就地驗證所有已到期 (target_date < today_str) 的 pending 預測：
    以該日「日彙總」的實測平均值為準計算誤差；該日無可比實測時標為 unverifiable。
    回傳本次驗證的筆數。
    """
    evaluated = 0
    for p in preds:
        if p.get("status") != "pending" or p.get("target_date", "9999") >= today_str:
            continue
        day = summaries.get(p["target_date"], {})
        stat = day.get(p["metric"])
        if stat and "mean" in stat:
            actual = stat["mean"]
            p["actual_value"] = actual
            p["error"] = round(actual - p["predicted_value"], 2)
            p["status"] = "evaluated"
        else:
            p["status"] = "unverifiable"
            p["actual_value"] = None
        evaluated += 1
    return evaluated


def format_prediction_feedback(preds: list, n=8) -> str:
    """格式化最近的預測校驗結果，供注入 prompt 形成自我校正迴路。"""
    done = [p for p in preds if p.get("status") == "evaluated"][-n:]
    pending = [p for p in preds if p.get("status") == "pending"]
    lines = ["【預測校驗回饋 (你過往的預測 vs 實測平均)】"]
    if not done:
        lines.append("- 尚無已驗證的預測。")
    for p in done:
        sign = "+" if p["error"] >= 0 else ""
        label = METRIC_LABELS.get(p["metric"], p["metric"])
        lines.append(
            f"- {p['target_date']} {label}: 預測 {p['predicted_value']} / 實測 {p['actual_value']} "
            f"(誤差 {sign}{p['error']}) — {p.get('note', '')}"
        )
    if pending:
        lines.append(f"（另有 {len(pending)} 筆預測待驗證）")
    return "\n".join(lines)
