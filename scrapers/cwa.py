# ======================================================================
# CWA 中央氣象署擷取：文山區七日預報爬蟲 + 文山站即時觀測 API (ET₀ 輸入)
# ======================================================================
import json
import threading

from playwright.sync_api import sync_playwright

from config import CWA_API_KEY, CWA_OBS_API, CWA_STATION_ID, redact
from logging_setup import logger
from science.et0 import calculate_et0, safe_float as _safe_float
from scrapers.waits import smart_wait
from watchdog import report_scraper_result

# 與阿龜分開兩把鎖，保留兩者「並行爬取」的原始設計，
# 僅防止「同一個」爬蟲被重複併發啟動。
CWA_SCRAPER_LOCK = threading.Lock()


def get_cwa_weather_forecast() -> str:
    """
    連線至中央氣象署網站，抓取台北市文山區未來七天的詳細天氣預報，包含最高氣溫、最低氣溫與天氣狀態。
    這個工具不需任何輸入參數。
    
    Returns:
        str: 台北市文山區未來七天的天氣預報文字摘要，若抓取失敗則回傳錯誤說明。
    """
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
    from storage.state import active_crops, load_state

    state = load_state()
    crops = active_crops(state)
    if not crops:
        return ""
    lines = ["- 各作物今日需水 ETc（= ET₀ × 作物係數 Kc，Kc 由 GDD 推估的生長階段決定）："]
    for crop in crops:
        cd = state.get("crops", {}).get(crop, {})
        _, info = lookup_crop_info(crop)
        target = info.get("target_gdd", 1000.0)
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
