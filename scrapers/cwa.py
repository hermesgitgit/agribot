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
# CWA 中央氣象署擷取：文山區七日預報爬蟲 + 文山站即時觀測 API (ET₀ 輸入)
# ======================================================================
import json
import threading

from playwright.sync_api import sync_playwright

from config import (CWA_API_KEY, CWA_HAZARD_API, CWA_HAZARD_LOCATION, CWA_OBS_API, CWA_STATION_ID,
                    CWA_TYPHOON_API, redact)
from logging_setup import logger
from science.et0 import calculate_et0, safe_float as _safe_float
from scrapers.waits import smart_wait
from watchdog import report_scraper_result

# 與阿龜分開兩把鎖，保留兩者「並行爬取」的原始設計，
# 僅防止「同一個」爬蟲被重複併發啟動。
CWA_SCRAPER_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# 警特報（颱風警報、豪雨/大雨特報、強風、低溫…）— 開放資料 JSON，數秒回應
# ----------------------------------------------------------------------
# 一週預報頁的文字只有「陰短暫雨、21~29 度」，颱風與特報從來不會出現在那裡；
# has_severe_weather() 掃預報文字因此在颱風天也掃不到關鍵字（2026-10-23 薔琵颱風實例）。
# 這裡另抓警特報，供：(1) 預報工具回傳開頭先列生效中警示；(2) 哨兵每小時掃描主動推播；
# (3) AI 直接查詢。10 分鐘快取，避免哨兵與對話同時打 API。
_ALERT_CACHE = {"ts": 0.0, "data": None}
_ALERT_CACHE_TTL = 600


def _cwa_get_json(url: str, params: dict) -> dict:
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"Authorization": CWA_API_KEY, "format": "JSON", **params})
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "agriweather-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fmt_time(s):
    """'2026-10-23 18:15:00' / '2026-10-23T18:15:00+08:00' → '10/23 18:15'；格式不符原樣回。"""
    s = str(s or "")
    try:
        core = s.replace("T", " ")[:16]
        return f"{int(core[5:7])}/{int(core[8:10])} {core[11:16]}"
    except (ValueError, IndexError):
        return s


def parse_cwa_alerts(hazard_json: dict, typhoon_json: dict) -> dict:
    """純函數：把兩個資料集的原始 JSON 整理成 {hazards: [...], typhoons: [...]}。
    hazards 每筆 {phenomena, significance, start, end}；typhoons 每筆
    {no, name, cname, time, lat, lon, wind, gust, pressure, moving}（取最新一筆分析定位）。
    欄位缺漏一律容忍（回空清單而非拋錯），供測試與離線重現。"""
    hazards = []
    for loc in (hazard_json or {}).get("records", {}).get("location") or []:
        for h in ((loc.get("hazardConditions") or {}).get("hazards") or []):
            info = h.get("info") or {}
            vt = h.get("validTime") or {}
            ph = str(info.get("phenomena") or "").strip()
            if not ph:
                continue
            hazards.append({"phenomena": ph,
                            "significance": str(info.get("significance") or "").strip(),
                            "start": _fmt_time(vt.get("startTime")),
                            "end": _fmt_time(vt.get("endTime"))})
    typhoons = []
    tcs = ((typhoon_json or {}).get("records", {}).get("TropicalCyclones") or {}).get("TropicalCyclone") or []
    for tc in tcs:
        fixes = ((tc.get("AnalysisData") or {}).get("Fix") or [])
        last = fixes[-1] if fixes else {}
        typhoons.append({"no": tc.get("CwaTyNo"), "name": tc.get("TyphoonName"),
                         "cname": tc.get("CwaTyphoonName"),
                         "time": _fmt_time(last.get("DateTime")),
                         "lat": last.get("CoordinateLatitude"), "lon": last.get("CoordinateLongitude"),
                         "wind": last.get("MaxWindSpeed"), "gust": last.get("MaxGustSpeed"),
                         "pressure": last.get("Pressure"),
                         "moving": f"{last.get('MovingDirection') or '?'} {last.get('MovingSpeed') or '?'} km/h"})
    return {"hazards": hazards, "typhoons": typhoons}


def format_cwa_alerts(alerts: dict, location: str = CWA_HAZARD_LOCATION) -> str:
    """純函數：整理成給 AI／使用者看的文字。
    無警示時的措辭刻意避開「颱風／豪雨／特報／警報」等字，否則 has_severe_weather() 會誤觸發。"""
    hz = (alerts or {}).get("hazards") or []
    ty = (alerts or {}).get("typhoons") or []
    if not hz and not ty:
        return f"【氣象警示】目前{location}沒有生效中的項目，西北太平洋也沒有活動中的熱帶氣旋。"
    lines = [f"【氣象警示｜{location}生效中】"]
    for h in hz:
        lines.append(f"- {h['phenomena']}{h['significance']}（{h['start']} 至 {h['end']}）")
    if not hz:
        lines.append(f"- {location}目前沒有生效中的特報或警報")
    for t in ty:
        lines.append(
            f"- 颱風現況：第 {t.get('no') or '?'} 號 {t.get('cname') or ''}（{t.get('name') or ''}）"
            f" {t.get('time') or ''} 中心 {t.get('lat')}N {t.get('lon')}E、近中心最大風速 {t.get('wind')} m/s"
            f"（陣風 {t.get('gust')}）、中心氣壓 {t.get('pressure')} hPa、移動 {t.get('moving')}")
    return "\n".join(lines)


def fetch_cwa_alerts(force: bool = False) -> dict:
    """抓兩個資料集並解析（10 分鐘快取）。沒金鑰或失敗回 {"error": ...}；部分成功仍回有拿到的那半。"""
    import time as _time
    now = _time.time()
    if not force and _ALERT_CACHE["data"] is not None and now - _ALERT_CACHE["ts"] < _ALERT_CACHE_TTL:
        return _ALERT_CACHE["data"]
    if not CWA_API_KEY:
        return {"error": "未設定 CWA_API_KEY，無法查詢警特報"}
    hazard_json, typhoon_json, errs = {}, {}, []
    try:
        hazard_json = _cwa_get_json(CWA_HAZARD_API, {"locationName": CWA_HAZARD_LOCATION})
    except Exception as e:
        errs.append(f"特報 API：{redact(e)}")
    try:
        typhoon_json = _cwa_get_json(CWA_TYPHOON_API, {})
    except Exception as e:
        errs.append(f"颱風 API：{redact(e)}")
    if not hazard_json and not typhoon_json:
        logger.error(f"❌ [CWA Alerts] 警特報 API 全部失敗：{'；'.join(errs)}")
        return {"error": "；".join(errs)}
    data = parse_cwa_alerts(hazard_json, typhoon_json)
    if errs:
        data["partial"] = "；".join(errs)
        logger.warning(f"⚠️ [CWA Alerts] 警特報 API 部分失敗：{data['partial']}")
    _ALERT_CACHE.update(ts=now, data=data)
    logger.info(f"✅ [CWA Alerts] 警特報取得：{len(data['hazards'])} 項特報、{len(data['typhoons'])} 個熱帶氣旋")
    return data


def get_cwa_alerts_report() -> str:
    """給 AI 工具與哨兵用的文字版；API 失敗時誠實說明而非回「無警示」。"""
    data = fetch_cwa_alerts()
    if data.get("error"):
        return f"【氣象警示】查詢失敗（{data['error']}），目前無法確認有無生效中的氣象警示，請以氣象署公告為準。"
    text = format_cwa_alerts(data)
    if data.get("partial"):
        text += f"\n（部分資料集查詢失敗：{data['partial']}）"
    return text


def get_cwa_weather_forecast() -> str:
    """
    連線至中央氣象署網站，抓取台北市文山區未來七天的詳細天氣預報，包含最高氣溫、最低氣溫與天氣狀態。
    回傳開頭會先附上「目前生效中的警特報」（颱風警報、豪雨特報等）——一週預報文字本身不含這些。
    這個工具不需任何輸入參數。
    
    Returns:
        str: 警特報摘要＋台北市文山區未來七天的天氣預報文字，若抓取失敗則回傳錯誤說明。
    """
    alerts_text = get_cwa_alerts_report()
    forecast_text = _scrape_cwa_forecast()
    return f"{alerts_text}\n\n【一週預報｜文山區】\n{forecast_text}"


def _scrape_cwa_forecast() -> str:
    logger.info("🌤️ [Pre-fetch] 正在執行 get_cwa_weather_forecast() 抓取氣象署預報...")
    try:
        # 持有 CWA 專屬鎖（與阿龜分開兩把鎖，保留兩者「並行爬取」的原始設計，
        # 僅防止「同一個」爬蟲被重複併發啟動）。
        with CWA_SCRAPER_LOCK, sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context()
            cwa_page = context.new_page()
            cwa_page.goto("https://www.cwa.gov.tw/V8/C/W/Town/Town.html?TID=6300800", wait_until="networkidle", timeout=15000)
            # networkidle 不保證預報元素已渲染完成——等待實際要抓取的錨點出現再提取
            smart_wait(cwa_page, 'a[name="T6300800"]', timeout_ms=10000, fallback_ms=2000, desc="CWA 預報元素")
            
            cwa_weather_array = cwa_page.evaluate("""() => { 
                return Array.from(document.querySelectorAll('a[name="T6300800"]'))
                    .map(a => {
                        let label = a.getAttribute('aria-label') || '';
                        let time = a.getAttribute('data-time') || '';
                        let tempEl = a.querySelector('.tem-C.is-active');
                        let tempText = tempEl ? tempEl.textContent.trim() : '';
                        
                        let formattedTime = time;
                        if (time.length >= 5) {
                            let month = time.slice(0, 2);
                            let day = time.slice(2, 4);
                            let period = time.slice(4) === 'D' ? '白天' : (time.slice(4) === 'N' ? '晚上' : time.slice(4));
                            formattedTime = `${month}/${day} ${period}`;
                        }
                        
                        let cleanLabel = label.replace('，請點此觀看文山區詳細天氣內容。', '');
                        if (tempText) {
                            return `${formattedTime}: ${cleanLabel} (溫度: ${tempText}°C)`;
                        } else {
                            return `${formattedTime}: ${cleanLabel}`;
                        }
                    })
                    .filter(a => a);
            }""")
            
            browser.close()
            
            cwa_weather_text = "\n".join(cwa_weather_array)
            if not cwa_weather_text.strip():
                report_scraper_result("cwa", False)
                res = "無法取得天氣預報，網頁中未發現資料。"
            else:
                report_scraper_result("cwa", True)
                res = cwa_weather_text
            
            logger.info("✅ [Pre-fetch] get_cwa_weather_forecast() 執行成功")
            return res
            
    except Exception as e:
        logger.error(f"❌ [Pre-fetch] 抓取氣象署資料失敗：{e}")
        report_scraper_result("cwa", False)
        return f"無法取得天氣預報，錯誤原因：{e}"


def fetch_cwa_observation() -> dict:
    """
    從 CWA O-A0001-001 取得文山站即時觀測，回傳整理後的字典：
    {air_temperature, relative_humidity, wind_speed, air_pressure,
     sunshine_duration, precipitation, obs_time}，無效值為 None。
    需要環境變數 CWA_API_KEY。
    """
    if not CWA_API_KEY:
        logger.warning("⚠️ [CWA Obs] 未設定 CWA_API_KEY 環境變數，無法取得觀測資料。")
        return {}
    import urllib.request
    import urllib.parse
    params = urllib.parse.urlencode({
        "Authorization": CWA_API_KEY,
        "StationId": CWA_STATION_ID,
        "format": "JSON",
    })
    url = f"{CWA_OBS_API}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agriweather-bot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"❌ [CWA Obs] 觀測 API 請求失敗: {redact(e)}")
        report_scraper_result("cwa_obs", False)
        return {}

    stations = data.get("records", {}).get("Station") or data.get("records", {}).get("location") or []
    if not stations:
        logger.warning("⚠️ [CWA Obs] 觀測 API 回應中找不到測站資料。")
        report_scraper_result("cwa_obs", False)
        return {}

    st = stations[0]
    we = st.get("WeatherElement", {})
    obs_time = (st.get("ObsTime", {}) or {}).get("DateTime", "未知")

    # 新版巢狀結構
    result = {
        "air_temperature": _safe_float(we.get("AirTemperature")),
        "relative_humidity": _safe_float(we.get("RelativeHumidity")),
        "wind_speed": _safe_float(we.get("WindSpeed")),
        "air_pressure": _safe_float(we.get("AirPressure")),
        "sunshine_duration": _safe_float(we.get("SunshineDuration")),
        "precipitation": _safe_float((we.get("Now", {}) or {}).get("Precipitation")),
        "obs_time": obs_time,
    }
    report_scraper_result("cwa_obs", True)
    logger.info(f"✅ [CWA Obs] 文山站觀測取得成功 @ {obs_time}")
    return result


def _crop_water_demand_block(et0_mm) -> str:
    """
    對每個在種作物，用其 GDD 進度推階段、配 Kc 算今日作物需水 ETc = ET₀ × Kc。
    這是 GDD（發育階段）與 ET₀（大氣需水）的搭配：同一份 ET₀ 因各作物階段不同而
    得出不同的實際需水。回傳可附加在 ET₀ 報告後的文字段落（無在種作物時回空字串）。
    延遲匯入 storage/science 以免 scraper 載入期耦合。
    """
    from science.gdd import lookup_crop_info
    from science.water import crop_water_demand
    from storage.state import active_crops, crop_target_gdd, load_state

    state = load_state()
    crops = active_crops(state)
    if not crops:
        return ""
    lines = ["- 各作物今日需水 ETc（= ET₀ × 作物係數 Kc，Kc 由 GDD 推估的生長階段決定）："]
    for crop in crops:
        cd = state.get("crops", {}).get(crop, {})
        _, info = lookup_crop_info(crop)
        target = crop_target_gdd(state, crop, info)
        r = crop_water_demand(crop, cd.get("accumulated_gdd", 0.0), target, et0_mm)
        etc_text = f"ETc ≈ {r['etc']} mm/日" if r["etc"] is not None else "ETc 無法計算"
        lines.append(f"  · {crop}：{r['stage']}、Kc≈{r['kc']} → {etc_text}")
    lines.append("  （ETc 為階段化的需水估算，不確定性高於 ET₀，仍以土壤濕度實測為最終裁判。）")
    return "\n" + "\n".join(lines)


def get_et0_report() -> str:
    """取得文山站即時觀測並計算 ET₀，回傳給 AI / 使用者的文字報告（含各在種作物的需水 ETc）。"""
    obs = fetch_cwa_observation()
    if not obs:
        return "【ET₀ 蒸散量】目前無法取得文山站觀測資料（請確認 CWA_API_KEY 設定與測站狀態）。"
    res = calculate_et0(obs)
    if res["et0"] is None:
        return f"【ET₀ 蒸散量】無法計算：{res['quality_note']}"
    rain = obs.get("precipitation")
    rain_text = f"，今日降水 {rain} mm" if rain is not None else ""
    balance = ""
    if rain is not None:
        net = round(rain - res["et0"], 2)
        balance = f"\n- 粗略水分收支：降水 {rain} − 蒸散 {res['et0']} = {net} mm（負值代表淨失水）"
    crop_block = _crop_water_demand_block(res["et0"])
    return (
        f"【ET₀ 參考蒸散量 — 文山站實測推算】\n"
        f"- 觀測時間：{obs.get('obs_time', '未知')}\n"
        f"- 今日 ET₀ ≈ {res['et0']} mm/日{rain_text}{balance}\n"
        f"- 計算輸入：{res['inputs_used']}\n"
        f"- 數據成色：{res['quality_note']}{crop_block}\n"
        f"（註：ET₀ 為有物理依據的估算值而非直接量測，請結合土壤濕度實測綜合判斷。）"
    )
