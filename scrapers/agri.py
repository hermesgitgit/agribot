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
# 阿龜微氣候 IoT 爬蟲 (AgriWeather Scraper) - API 版
# ======================================================================
import datetime
import json
import re

import requests

from config import AGRI_API_KEY, AGRI_SUID, redact
from logging_setup import logger
from storage.history import save_to_history
from storage.temp_log import iso_to_taipei_str as _iso_to_taipei_str, merge_temp_log
from watchdog import report_scraper_result

# 土壤電導度 EC 專用的「邊界比對」：只認 ec 作為獨立 token（^ec / _ec / ec_）或
# conductivity，避免 ['ec'] 子字串誤抓 record / expected / second 等含 'ec' 的無關欄位
# （那會把垃圾值當成 EC，污染歷史、誤觸鹽害警報）。
_EC_PATTERN = re.compile(r'conduct|(?:^|[^a-z])ec(?:$|[^a-z])', re.IGNORECASE)


def _find_ec(data):
    """遞迴尋找土壤電導度 EC 數值，以 token 邊界比對；查無回 None。"""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (int, float, str)) and _EC_PATTERN.search(str(k)):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, (dict, list)):
                r = _find_ec(v)
                if r is not None:
                    return r
    elif isinstance(data, list):
        for item in reversed(data):
            r = _find_ec(item)
            if r is not None:
                return r
    return None

def _find_metric_in_dict(data, keywords):
    """
    強健的遞迴搜尋：在未知的 JSON 結構中，尋找 Key 包含指定關鍵字的數值。
    例如 keywords=['air', 'temp'] 會命中 'air_temperature', 'AirTemp', 等。
    """
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (int, float, str)):
                k_lower = str(k).lower()
                if all(kw in k_lower for kw in keywords):
                    try:
                        return float(v)
                    except ValueError:
                        pass
            elif isinstance(v, (dict, list)):
                res = _find_metric_in_dict(v, keywords)
                if res is not None:
                    return res
    elif isinstance(data, list) and len(data) > 0:
        # 反轉搜尋最新(最後)的元素
        for item in reversed(data):
            res = _find_metric_in_dict(item, keywords)
            if res is not None:
                return res
    return None

def format_6h_history(history_list) -> str:
    """
    將官方 API 的歷史區間資料格式化。
    """
    if not history_list or not isinstance(history_list, list) or len(history_list) == 0:
        return "【過去 6 小時歷史感測數據趨勢】：未成功獲取或無數據。"

    try:
        times, air_temps, air_hums, soil_temps, soil_moistures, soil_conductivities = [], [], [], [], [], []
        
        for point in history_list:
            t_str = str(point)
            if isinstance(point, dict):
                for tk in ['time', 'timestamp', 'date', 'created_at']:
                    if tk in point:
                        t_str = str(point[tk])
                        break
            
            times.append(t_str)
            air_temps.append(_find_metric_in_dict(point, ['air', 'temp']))
            air_hums.append(_find_metric_in_dict(point, ['air', 'hum']))
            soil_temps.append(_find_metric_in_dict(point, ['soil', 'temp']))
            soil_moistures.append(_find_metric_in_dict(point, ['soil', 'moist']) or _find_metric_in_dict(point, ['soil', 'hum']) or _find_metric_in_dict(point, ['soil', 'water']))
            soil_conductivities.append(_find_ec(point))
            
        n_points = len(times)
        
        def format_iso_time(iso_str):
            full = _iso_to_taipei_str(iso_str)
            return full[11:] if full else iso_str[-8:]

        start_time_local = format_iso_time(times[0])
        end_time_local = format_iso_time(times[-1])

        def get_stats(arr):
            clean = [x for x in arr if x is not None]
            if not clean:
                return "無資訊", "無資訊", "無資訊", "無資訊"
            return clean[0], clean[-1], max(clean), min(clean)

        s_at, e_at, max_at, min_at = get_stats(air_temps)
        s_ah, e_ah, max_ah, min_ah = get_stats(air_hums)
        s_st, e_st, max_st, min_st = get_stats(soil_temps)
        s_sm, e_sm, max_sm, min_sm = get_stats(soil_moistures)
        s_sc, e_sc, max_sc, min_sc = get_stats(soil_conductivities)

        summary = (
            f"【過去歷史數據趨勢 (台北時間 {start_time_local} ~ {end_time_local}，共 {n_points} 筆)】\n"
            f"- 🌡️ 空氣溫度：從 {s_at} ℃ 變為 {e_at} ℃ (區間最高 {max_at} ℃, 最低 {min_at} ℃)\n"
            f"- 💧 空氣濕度：從 {s_ah} % 變為 {e_ah} % (區間最高 {max_ah} %, 最低 {min_ah} %)\n"
            f"- 🎚️ 土壤溫度：從 {s_st} ℃ 變為 {e_st} ℃ (區間最高 {max_st} ℃, 最低 {min_st} ℃)\n"
            f"- 🌊 土壤濕度：從 {s_sm} % 變為 {e_sm} % (區間最高 {max_sm} %, 最低 {min_sm} %)\n"
            f"- 🧪 土壤 EC 值：從 {s_sc} 變為 {e_sc} (區間最高 {max_sc}, 最低 {min_sc})\n"
        )
        return summary
    except Exception as parse_err:
        return f"【歷史感測數據趨勢】：解析失敗 {parse_err}"

# API 端點 base 與設備參數清單（/parameters 回傳的感測項目名，作為 data 端點必填的 params）
_API_BASE = f"https://api.agriweather.com.tw/api/v2/serviceunits/{AGRI_SUID}" if AGRI_SUID else ""
_PARAM_CACHE = []
# 後備清單：/parameters 取不到時用，確保 data 端點仍能帶上必填的 params（順序不拘）
_DEFAULT_PARAMS = ["soil_temperature", "soil_moisture", "soil_conductivity", "air_temperature", "air_humidity"]
# API 回應欄位名 → 系統內部欄位名（土壤含水率在 API 叫 soil_moisture、EC 叫 soil_conductivity）
_FIELD_MAP = {
    "air_temperature": "air_temperature",
    "air_humidity": "air_humidity",
    "soil_temperature": "soil_temperature",
    "soil_moisture": "soil_humidity",
    "soil_conductivity": "soil_ec",
}


def _get_param_list(headers) -> list:
    """取得設備可用感測參數清單（GET /parameters，只需 api_token）；快取一次。
    失敗時退回已知預設清單——資料端點的 params 為必填，缺它會 422。"""
    global _PARAM_CACHE
    if _PARAM_CACHE:
        return _PARAM_CACHE
    try:
        r = requests.get(f"{_API_BASE}/parameters", headers=headers,
                         params={"api_token": AGRI_API_KEY}, timeout=10)
        if r.ok:
            body = r.json()
            pl = body.get("parameters") if isinstance(body, dict) else None
            if pl:
                _PARAM_CACHE = list(pl)
                logger.info(f"✅ [Agri API] 設備可用參數：{_PARAM_CACHE}")
                return _PARAM_CACHE
    except Exception as e:
        logger.warning(f"⚠️ [Agri API] 取得參數清單失敗，改用預設清單: {redact(e)}")
    return _DEFAULT_PARAMS


def _latest_point(data_list) -> dict:
    """從資料點清單取「time 最新」的一筆——realtime 是新→舊、history 是舊→新，
    一律以 time 取最大值最穩，不依賴排序方向。空清單回 {}。"""
    points = [p for p in (data_list or []) if isinstance(p, dict)]
    if not points:
        return {}
    return max(points, key=lambda p: p.get("time", ""))


def get_agriweather_data(include_advice: bool = False) -> str:
    """
    透過官方 API 抓取最新即時數據與歷史區間資料。
    流程：先取設備參數清單 → 帶 params（逗號字串）打 realtime 與 hourly-history。
    include_advice=True 時，額外以 Playwright 爬取阿龜原生灌溉/施肥建議（較慢、隔離，
    僅定時推播啟用；每小時哨兵與一般對話走快速 API、不爬建議）。
    """
    logger.info("📡 [Agri API] 正在透過官方 API 抓取感測數據...")
    
    if not AGRI_API_KEY or not AGRI_SUID:
        logger.error("❌ 缺少 AGRI_API_KEY 或 AGRI_SUID 環境變數！")
        return "無法獲取資料，系統缺少 API 金鑰設定。"

    headers = {"Authorization": f"Bearer {AGRI_API_KEY}", "Accept": "application/json"}
    base_url = _API_BASE

    try:
        # 阿龜 API 規格（已實測）：start_time/end_time 須為 ISO 8601 UTC（含字面 T 與 Z），
        # 且 params 為「逗號分隔的字串」（陣列或 JSON 都會被拒）。Z 代表 UTC，故用 UTC 時間。
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ISO = "%Y-%m-%dT%H:%M:%SZ"
        param_str = ",".join(_get_param_list(headers))
        base_q = {"api_token": AGRI_API_KEY, "params": param_str}

        hist_list, rt_list = [], []

        # 1. 歷史資料（供 GDD 回補 24h 氣溫 + 過去趨勢摘要）
        logger.info(f"📡 呼叫歷史資料 API: {base_url}/hourly-history")
        hq = dict(base_q,
                  start_time=(now_utc - datetime.timedelta(hours=24)).strftime(ISO),
                  end_time=now_utc.strftime(ISO))
        hist_res = requests.get(f"{base_url}/hourly-history", headers=headers, params=hq, timeout=10)
        if hist_res.ok:
            hd = hist_res.json()
            hist_list = hd.get("data", []) if isinstance(hd, dict) else (hd if isinstance(hd, list) else [])
            if hist_list:
                legacy_format = {"api_device": {
                    "time": [str(p.get("time", "")) for p in hist_list],
                    "air_temperature": [p.get("air_temperature") for p in hist_list]}}
                merged = merge_temp_log(legacy_format)
                logger.info(f"✅ 成功寫入 {merged} 筆歷史氣溫點至 GDD 引擎。")
        else:
            logger.warning(f"⚠️ 歷史 API 請求失敗 HTTP {hist_res.status_code}:\n{redact(hist_res.text)}")

        # 2. 即時資料（取最新一筆）。realtime 同樣要求 start_time/end_time（缺則 422），
        # 故沿用 hq（含日期）；它回 10 分鐘間隔的近期點，比歷史的每小時更新鮮。
        logger.info(f"📡 呼叫即時資料 API: {base_url}/realtime")
        rt_res = requests.get(f"{base_url}/realtime", headers=headers, params=hq, timeout=10)
        if rt_res.ok:
            rd = rt_res.json()
            rt_list = rd.get("data", []) if isinstance(rd, dict) else (rd if isinstance(rd, list) else [])
        else:
            logger.warning(f"⚠️ 即時 API 請求失敗 HTTP {rt_res.status_code}:\n{redact(rt_res.text)}")

        # 取最新一筆：即時優先，缺則退回歷史最後一筆（皆以 time 取最新，不依賴排序方向）
        point = _latest_point(rt_list) or _latest_point(hist_list)
        sensors_scraped_ok = bool(point)
        if not sensors_scraped_ok:
            logger.warning("⚠️ 兩支 API 皆無法取得有效數據。")

        # 欄位對映：只收數值型，非數值（None/字串）視為無資訊
        def _num(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        scraped = {sys_key: _num(point.get(api_key)) for api_key, sys_key in _FIELD_MAP.items()}

        report_scraper_result("agri", sensors_scraped_ok)

        # 原生灌溉/施肥建議（僅 include_advice 時，以隔離的 Playwright 爬取；
        # 失敗只回退化字串、絕不影響上方已取得的感測數據）
        irrigation_advice = fertilization_advice = "（本輪未抓取原生建議）"
        if include_advice:
            try:
                from scrapers.agri_advice import fetch_native_advice
                irrigation_advice, fertilization_advice = fetch_native_advice()
            except Exception as adv_err:
                logger.warning(f"⚠️ [Agri API] 原生建議抓取失敗（不影響感測數據）: {redact(adv_err)}")

        def fmt(val, unit):
            return f"{val} {unit}" if val is not None else "無資訊"

        final_data = {
            "air_temperature": fmt(scraped.get("air_temperature"), "℃"),
            "air_humidity": fmt(scraped.get("air_humidity"), "%"),
            "soil_temperature": fmt(scraped.get("soil_temperature"), "℃"),
            "soil_humidity": fmt(scraped.get("soil_humidity"), "%"),
            "soil_ec": fmt(scraped.get("soil_ec"), "ds/m"),
            "irrigation_advice": irrigation_advice,
            "fertilization_advice": fertilization_advice,
            "past_6h_summary": format_6h_history(hist_list),
        }
        result = json.dumps(final_data, ensure_ascii=False)
        logger.info(f"✅ [Agri API] 擷取成功: {result}")

        if sensors_scraped_ok:
            try:
                save_to_history(final_data)
            except Exception as hist_err:
                logger.warning(f"⚠️ 儲存歷史數據時出錯: {hist_err}")
        return result

    except Exception as e:
        # 必須經 redact()：API 金鑰以 api_token 查詢參數帶入 URL，requests 的例外訊息
        # 常內嵌完整請求 URL，未遮罩會把金鑰寫進日誌、甚至隨錯誤訊息回傳給使用者。
        logger.error(f"❌ [Agri API] 抓取過程發生例外錯誤：{redact(e)}")
        report_scraper_result("agri", False)
        return f"無法獲取阿龜微氣候感測數據，錯誤原因：{redact(e)}"
