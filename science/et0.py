# ======================================================================
# ET₀ 蒸發散 — 純計算核心 (Science: FAO-56 Penman-Monteith)
# ======================================================================
# 由單點即時實測（觀測字典）推算每日參考作物蒸散量 ET₀。
# 誠實原則：日照時數→輻射採 Angström-Prescott 標準轉換、缺項時退化處理，
# 回傳值一律標注數據成色（哪些是實測、哪些經推估），不營造「精確的錯覺」。
# 本模組為純函數：觀測資料由呼叫端傳入（取得觀測在 scrapers/cwa.py）。
import datetime
import math

from config import CWA_STATION_LAT, TZ_TAIPEI


def safe_float(v):
    """將 CWA 數值轉 float；無效標記（-99/-990 等）回傳 None。"""
    try:
        f = float(v)
        return None if f <= -90 else f
    except (TypeError, ValueError):
        return None


def _saturation_vapour_pressure(t_c):
    """飽和水氣壓 es (kPa)，FAO-56 式 11。"""
    return 0.6108 * math.exp((17.27 * t_c) / (t_c + 237.3))


def calculate_et0(obs: dict, target_date=None) -> dict:
    """
    以 FAO-56 Penman-Monteith 推算每日參考蒸散量 ET₀ (mm/day)。
    輸入為 fetch_cwa_observation() 的回傳字典。
    回傳 {et0, quality_note, inputs_used}；資料不足時 et0 為 None。

    誠實標注：
    - 日照時數→太陽輻射採 Angström-Prescott (a_s=0.25, b_s=0.50, FAO 預設)。
    - 風速為單一時刻觀測值（非全日平均），作為近似。
    - 缺風速時退用 2 m/s（FAO 建議的全球平均），並在 note 標明。
    """
    if target_date is None:
        target_date = datetime.datetime.now(TZ_TAIPEI)

    t = obs.get("air_temperature")
    rh = obs.get("relative_humidity")
    sun_hours = obs.get("sunshine_duration")
    p_kpa = (obs.get("air_pressure") or 1013.0) / 10.0  # hPa → kPa
    u_raw = obs.get("wind_speed")

    notes = []
    if t is None or rh is None:
        return {"et0": None,
                "quality_note": "氣溫或濕度缺值，無法計算 ET₀。",
                "inputs_used": obs}

    # 風速：CWA 觀測高度約 10m，PM 式需 2m 高風速，套用 FAO-56 對數風廓線換算
    if u_raw is None:
        u2 = 2.0
        notes.append("風速缺值，採 FAO 全球平均 2 m/s")
    else:
        u2 = u_raw * 4.87 / math.log(67.8 * 10.0 - 5.42)
        notes.append(f"風速由 10m 實測 {u_raw} m/s 換算至 2m")

    # 飽和與實際水氣壓
    es = _saturation_vapour_pressure(t)
    ea = es * (rh / 100.0)
    delta = 4098 * es / ((t + 237.3) ** 2)  # 飽和水氣壓曲線斜率
    gamma = 0.000665 * p_kpa                 # 濕度計常數

    # 地球外輻射 Ra（依緯度與日序）
    lat_rad = math.radians(CWA_STATION_LAT)
    J = target_date.timetuple().tm_yday
    dr = 1 + 0.033 * math.cos(2 * math.pi / 365 * J)
    decl = 0.409 * math.sin(2 * math.pi / 365 * J - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(decl))))
    Ra = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(lat_rad) * math.sin(decl) +
        math.cos(lat_rad) * math.cos(decl) * math.sin(ws))
    N = 24 / math.pi * ws  # 最大可能日照時數

    # 日照時數 → 太陽輻射 Rs（Angström-Prescott）
    if sun_hours is None:
        # 退化：以 Hargreaves 概念用 Ra 粗估，標明高度不確定
        Rs = 0.16 * Ra * 0.7  # 粗略折減
        notes.append("日照時數缺值，輻射改用粗估（不確定性高）")
    else:
        Rs = (0.25 + 0.50 * min(sun_hours / N, 1.0)) * Ra
        notes.append(f"輻射由日照時數 {sun_hours}hr 經 Angström 轉換")

    # 淨輻射 Rn
    Rso = (0.75 + 2e-5 * 40) * Ra  # 海拔 40m
    Rns = (1 - 0.23) * Rs          # 反照率 0.23
    sigma = 4.903e-9
    tk = (t + 273.16) ** 4
    Rnl = sigma * tk * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * min(Rs / Rso, 1.0) - 0.35)
    Rn = Rns - Rnl
    G = 0  # 日尺度土壤熱通量近似為 0

    # Penman-Monteith (FAO-56 式 6)
    numerator = 0.408 * delta * (Rn - G) + gamma * (900 / (t + 273)) * u2 * (es - ea)
    denominator = delta + gamma * (1 + 0.34 * u2)
    et0 = numerator / denominator
    et0 = round(max(0.0, et0), 2)

    return {
        "et0": et0,
        "quality_note": "；".join(notes),
        "inputs_used": {
            "氣溫": t, "相對濕度": rh, "風速2m": round(u2, 2),
            "日照時數": sun_hours, "氣壓hPa": obs.get("air_pressure"),
        }
    }
