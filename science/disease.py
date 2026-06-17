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
# 病害風險預警 — 純邏輯核心 (Science: Foliar Disease Pressure)
# ======================================================================
# 葉菜的葉部病害由「葉面持續潮濕 × 溫度落在病菌適溫區」驅動，但其實有兩條
# 不同的傳播路徑，本模組分開建模——讓系統能在病斑「之前」預警（事前 vs 事後）：
#
#   (A) 葉面潮濕路徑：空氣濕度/結露時數 → 真菌·卵菌孢子萌發侵染。
#       霧、結露都算數（要的是「葉子濕多久」）。對應 露菌病、白銹病、灰黴病。
#   (B) 雨水濺潑路徑：實際降雨打地 → 濺起土中病菌附著到下位葉。
#       「霧再濃也濺不起泥土」，這條要的是「有沒有真的下雨」＋夠暖。
#       對應土媒/濺潑型的 軟腐病、炭疽病——故這兩者只在『有降雨』時才列入，
#       不再被單純的高濕（霧）誤判觸發。
#
# 誠實原則：這是「基於氣象條件的風險指標」，不是診斷。雨量來自鄰近的 CWA
# 文山站、屬鄰近估計（園內濕度才是實測），故當佐證而非鐵證。具體防治建議由
# AI 結合知識庫給出。本模組為純函數：不讀 DB/state，輸入由呼叫端傳入。

# 高濕門檻：空氣濕度 ≥ 此值視為「葉面易結露/持續潮濕」（葉部病害的關鍵驅動）
HIGH_HUMIDITY_PCT = 90.0
# 近 24h 高濕時數達此值 → 葉面長時間潮濕，風險顯著升高
WET_HOURS_ELEVATED = 6
WET_HOURS_HIGH = 12

# 降雨門檻（mm，近 24h）：可測降雨即有濺潑、較大雨量濺潑更強
RAIN_LIGHT_MM = 1.0
RAIN_HEAVY_MM = 10.0
# 濺潑型土媒病害（軟腐/炭疽）需足夠溫暖才活躍；低於此溫即使下雨也不列入
SPLASH_MIN_TEMP = 18.0

# 溫度適病區 → 葉面潮濕路徑的好發病害（粗分兩段；範圍為常見經驗值）
_COOL_BAND = (10.0, 25.0)   # 涼至溫和：露菌病、白銹病、灰黴病
_WARM_BAND = (25.0, 34.0)   # 偏暖：白銹病
_COOL_FOLIAR = ["露菌病", "白銹病", "灰黴病"]
_WARM_FOLIAR = ["白銹病"]
# 雨水濺潑路徑的好發病害（需實際降雨 + 夠暖才列入）
_SPLASH_DISEASES = ["軟腐病", "炭疽病"]


def assess_disease_risk(air_temp, air_humidity, high_humidity_hours,
                        recent_rain_mm=0.0, leafy=True) -> dict:
    """
    由當前氣溫、空氣濕度、近 24h 高濕時數與近 24h 雨量，評估葉部病害壓力。
    回傳 {level, score, diseases, reasons}：
      level ∈ {低, 中, 高}；diseases 為候選病害（葉面潮濕型＋有雨時的濺潑型）；
      reasons 為觸發說明。
    雙路徑：濕度/高濕時數驅動葉面潮濕型（露菌/白銹/灰黴）；recent_rain_mm 驅動
    濺潑型土媒病害（軟腐/炭疽，僅在有降雨且夠暖時列入——霧/高濕不算）。
    leafy=True 表示在種葉菜（密植、嫩葉，較易感）——非葉菜時略降權重。
    資料不足（濕度或氣溫缺值）時回 level=未知，不妄下結論。雨量缺值以 0 處理。
    """
    if air_humidity is None or air_temp is None:
        return {"level": "未知", "score": 0, "diseases": [],
                "reasons": ["缺少濕度或氣溫實測，本次不評估病害風險。"]}

    score = 0
    reasons = []

    # 1) 濕度本身（葉面結露的即時驅動）
    if air_humidity >= 95:
        score += 2
        reasons.append(f"空氣濕度極高（{air_humidity}%），葉面易長時間結露")
    elif air_humidity >= HIGH_HUMIDITY_PCT:
        score += 1
        reasons.append(f"空氣濕度偏高（{air_humidity}%）")

    # 2) 高濕持續時數（葉面潮濕時間越長，孢子萌發/侵染機會越大）
    h = high_humidity_hours or 0
    if h >= WET_HOURS_HIGH:
        score += 2
        reasons.append(f"近 24h 有 {h} 小時處於高濕（葉面長時間潮濕）")
    elif h >= WET_HOURS_ELEVATED:
        score += 1
        reasons.append(f"近 24h 有 {h} 小時高濕")

    # 3) 雨水濺潑路徑（實際降雨；霧/高濕不算）
    rain = recent_rain_mm or 0.0
    splash = False
    if rain >= RAIN_HEAVY_MM:
        score += 2
        splash = True
        reasons.append(f"近 24h 雨量偏大（約 {rain} mm），雨滴濺潑強烈，易把土中病菌打上下位葉")
    elif rain >= RAIN_LIGHT_MM:
        score += 1
        splash = True
        reasons.append(f"近 24h 有可測降雨（約 {rain} mm），有雨水濺潑帶菌的風險")

    # 4) 溫度適病區 → 葉面潮濕型的好發病害
    diseases = []
    if _COOL_BAND[0] <= air_temp <= _COOL_BAND[1]:
        diseases = list(_COOL_FOLIAR)
        reasons.append(f"氣溫 {air_temp}°C 落在露菌/白銹/灰黴的適溫區")
    elif _WARM_BAND[0] < air_temp <= _WARM_BAND[1]:
        diseases = list(_WARM_FOLIAR)
        reasons.append(f"氣溫 {air_temp}°C 偏暖，利於白銹病")
    else:
        reasons.append(f"氣溫 {air_temp}°C 不在主要葉部病害的適溫區，葉面潮濕型風險受抑")

    # 5) 濺潑型土媒病害：需『實際降雨』且夠暖才列入（霧/高濕不觸發）
    if splash and air_temp >= SPLASH_MIN_TEMP:
        for d in _SPLASH_DISEASES:
            if d not in diseases:
                diseases.append(d)
        reasons.append("雨後偏暖，雨水濺潑易帶起土媒病菌，特別留意軟腐病／炭疽病")

    # 葉菜密植嫩葉較易感；非葉菜略降一級門檻感受
    if not leafy and score > 0:
        score -= 1

    # 綜合判級：需「致濕條件（高濕或降雨）」與「對應病害」同時成立才會到中/高
    if score >= 3 and diseases:
        level = "高"
    elif score >= 2 and diseases:
        level = "中"
    elif score >= 1:
        level = "低"
    else:
        level = "低"

    return {"level": level, "score": score, "diseases": diseases, "reasons": reasons}


def format_disease_report(risk: dict, crops=None) -> str:
    """把 assess_disease_risk 的結果格式化成可注入 prompt / 推播的文字。"""
    if risk["level"] == "未知":
        return "【病害風險】" + risk["reasons"][0]
    crop_txt = f"（在種作物：{', '.join(crops)}）" if crops else ""
    icon = {"高": "🔴", "中": "🟠", "低": "🟢"}.get(risk["level"], "⚪")
    lines = [f"【🦠 葉部病害風險：{icon} {risk['level']}】{crop_txt}"]
    for r in risk["reasons"]:
        lines.append(f"- {r}")
    # 候選病害僅在風險達中/高時才提示，避免低風險時的雜訊
    if risk["diseases"] and risk["level"] in ("中", "高"):
        lines.append(f"- 依目前條件較需留意：{'、'.join(risk['diseases'])}")
    lines.append("（此為氣象條件推估的風險指標，非診斷；具體防治請結合知識庫與現場觀察。）")
    return "\n".join(lines)
