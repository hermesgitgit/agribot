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
# Agentic 工具層 (Agent Tools)
# ======================================================================
# 這些函式會以 Gemini Function Calling 的形式註冊給模型，由模型在推理過程中
# 「自主決定」是否呼叫、呼叫哪些、以什麼順序呼叫（ReAct 迴路）。
# SDK 會自動從型別註記與 docstring 生成工具 schema，docstring 即是模型
# 看到的工具說明，務必準確描述用途、成本與回傳格式。
import re

from agent.guard import MAX_THRESHOLD_STEP
from agent.pending import _current_chat_ctx, _set_pending_event
from agent.prompts import build_disease_report, build_state_summary
from logging_setup import logger
from science.gdd import CROP_GDD_DATABASE, lookup_crop_info, match_crop_key
from science.vision import crosscheck_stage
from science.water import growth_stage
from scrapers.agri import get_agriweather_data
from scrapers.cwa import get_cwa_weather_forecast, get_et0_report
from storage.harvest import get_harvest_cadence_summary
from storage.knowledge import search_knowledge
from storage.history import load_history_summary
from storage.predictions import load_prediction_feedback, save_prediction
from storage.state import load_state, set_crop_tracking, update_state
from storage.summaries import load_daily_summaries
from storage.vision_log import load_visual_history, record_visual_assessment


def tool_get_realtime_sensor_data() -> str:
    """抓取阿龜物聯網的最新即時感測數據（氣溫、空氣濕度、土壤溫度、土壤含水率、土壤EC）與過去趨勢摘要。透過官方 API，數秒內回應、不需啟動瀏覽器；需要最新現場數據時即可呼叫，同一輪對話中切勿重複呼叫。（註：阿龜平台原生的灌溉/施肥文字建議僅在定時推播時提供，本工具不含。）回傳 JSON 字串。"""
    return get_agriweather_data()


def tool_get_weather_forecast() -> str:
    """抓取中央氣象署對使用者耕地所在地（台北市文山區）的最新天氣預報，包含溫度、降雨機率與天氣描述。耗時約30~60秒。判斷未來澆水排程、極端天氣防護時呼叫。回傳預報文字。"""
    return get_cwa_weather_forecast()


def tool_query_recent_history() -> str:
    """查詢最近 40 筆的原始感測歷史記錄（每筆含時間戳、氣溫、空氣濕度、土溫、土壤濕度、EC），適合分析最近數日內的短期趨勢。即時讀取本地檔案，無耗時。回傳 JSON 字串。"""
    return load_history_summary()


def tool_query_daily_summaries(days: int) -> str:
    """查詢最近 N 天的「日彙總」長期趨勢（每天一筆，各感測值的最低~最高/平均），適合進行土質蓄水力、肥料壽命等需要數週尺度的斜率分析。days 為要回顧的天數（1~60）。即時讀取本地檔案，無耗時。"""
    try:
        days = max(1, min(int(days), 60))
    except (TypeError, ValueError):
        days = 30  # 模型傳入非數值參數時退用預設值，不讓例外炸穿 AFC 迴路
    return load_daily_summaries(n=days)


def tool_get_garden_status() -> str:
    """查詢目前農園的監控設定與作物狀態：作物種類、生長階段、土壤濕度警戒門檻、累計生長積溫(GDD)與成熟目標。即時讀取本地檔案，無耗時。"""
    return build_state_summary()


def tool_set_thresholds(dry: float, wet: float, lifecycle_stage: str) -> str:
    """調整本地哨兵系統的土壤濕度警戒門檻。dry=乾燥警戒%（低於即告警）、wet=積水警戒%（高於即告警）、lifecycle_stage=生長階段代碼（如 seedling/vegetative/flowering/mature）。安全限制：必須 0 < dry < wet < 100 且單次調幅不得超過±15個百分點，違反時會被拒絕並回傳原因。成功或失敗都會回傳結果訊息，請據此告知使用者。"""
    try:
        new_dry = float(dry)
        new_wet = float(wet)
    except (TypeError, ValueError):
        return "❌ 設定失敗：dry 與 wet 必須是數值。"
    if not re.fullmatch(r'\w{1,30}', str(lifecycle_stage) or ""):
        return "❌ 設定失敗：lifecycle_stage 僅允許 1~30 個英數字元。"
    if not (0.0 < new_dry < new_wet < 100.0):
        return f"❌ 設定被拒絕：必須滿足 0 < dry({new_dry}) < wet({new_wet}) < 100。"
    def _apply(state):
        cur_dry = float(state.get("dry_threshold", 30.0))
        cur_wet = float(state.get("wet_threshold", 80.0))
        if abs(new_dry - cur_dry) > MAX_THRESHOLD_STEP or abs(new_wet - cur_wet) > MAX_THRESHOLD_STEP:
            return (f"❌ 設定被拒絕：單次調幅超過 ±{MAX_THRESHOLD_STEP} 百分點"
                    f"（目前 dry={cur_dry}, wet={cur_wet}）。請建議使用者以 /threshold 手動指令進行大幅調整。")
        state["lifecycle"] = f"{lifecycle_stage} (AI 自主評估生長階段)"
        state["dry_threshold"] = new_dry
        state["wet_threshold"] = new_wet
        return None

    reject_reason = update_state(_apply)
    if reject_reason:
        return reject_reason
    logger.info(f"🔄 [Agentic Closed-Loop / 工具呼叫] 閾值已更新: dry={new_dry}%, wet={new_wet}%, state={lifecycle_stage}")
    return f"✅ 警戒門檻已更新：乾燥 < {new_dry}% / 積水 > {new_wet}%，生長階段標記為 {lifecycle_stage}。"


def tool_set_crop(crop_name: str) -> str:
    """切換目前種植的作物（會自動模糊比對內建 GDD 資料庫：水稻/玉米/番茄/萵苣/草莓/馬鈴薯/高麗菜/小黃瓜/空心菜，比對不到則以自訂名稱用預設參數）。各作物的累計積溫獨立保存，切換不會清除。僅在使用者明確表示更換作物時呼叫。回傳切換結果。"""
    new_crop = str(crop_name).strip()[:40]
    if not new_crop:
        return "❌ 切換失敗：作物名稱不可為空。"
    matched = None
    for k in CROP_GDD_DATABASE.keys():
        if new_crop.split()[0] in k or k.split()[0] in new_crop:
            matched = k
            break
    if not matched:
        matched = new_crop
    def _apply(state):
        state["crop_name"] = matched
        entry = state.setdefault("crops", {}).setdefault(matched, {"accumulated_gdd": 0.0, "last_gdd_date": ""})
        entry["active"] = True  # 設為焦點即視為在種、納入每日 GDD 追蹤
        return entry.get("accumulated_gdd", 0.0)

    acc = update_state(_apply)
    logger.info(f"🔄 [Agentic Closed-Loop / 工具呼叫] 焦點作物已設為: {matched}")
    return f"✅ 焦點作物已設為：{matched}（該作物目前累計 GDD: {acc} ℃-day，並已納入每日追蹤）。"


def tool_track_crop(crop_name: str) -> str:
    """當使用者表達『他「同時、額外」也種了另一種作物』（例如「我這畦也種了萵苣」「旁邊還種了一排小白菜」），呼叫此工具把該作物加入每日 GDD 追蹤，與現有作物並行累積（各自獨立、共用同一塊地的溫度）。這不會改變對話焦點作物。立即生效（加入追蹤是低風險、可逆操作，無需確認）；呼叫後請在回覆中告知使用者已加入、並說明若是誤會可請他說「停止追蹤該作物」。同一則訊息提到多種作物時可分別呼叫本工具多次。切勿在使用者只是『詢問』『考慮』『提到別人種』時呼叫。"""
    crop = str(crop_name).strip()[:40]
    if not crop:
        return "（作物名稱不可為空）"
    matched, acc = set_crop_tracking(crop, True)
    logger.info(f"🌱 [Agentic Closed-Loop / 工具呼叫] 已加入每日 GDD 追蹤: {matched}")
    return f"✅ 已將「{matched}」加入每日 GDD 追蹤（目前累計 {acc} ℃-day），今晚 00:05 起一併結算。請告知使用者；若是誤會，可請他說「停止追蹤{matched}」即可移除。"


def tool_finish_crop(crop_name: str) -> str:
    """當使用者表達『某作物已「採收完畢、清園、拔除」、不再種植』（例如「空心菜整畦拔光了」「番茄這季收完清掉了」），呼叫此工具停止該作物的每日 GDD 累積（保留歷史積溫，僅不再往上加）。立即生效（停止追蹤可逆，無需確認）；呼叫後請在回覆中告知使用者已停止、歷史積溫仍保留。切勿在使用者只是『割收一次但繼續種』（那是 tool_record_harvest_event）或只是討論時呼叫。"""
    crop = str(crop_name).strip()[:40]
    if not crop:
        return "（作物名稱不可為空）"
    matched, acc = set_crop_tracking(crop, False)
    if acc is None:
        return f"⚠️ 找不到作物「{matched}」，未做變更。可請使用者用 /crops 查看目前追蹤清單。"
    logger.info(f"🌿 [Agentic Closed-Loop / 工具呼叫] 已停止追蹤: {matched}")
    return f"✅ 已停止追蹤「{matched}」（保留歷史累計 {acc} ℃-day，不再每日累加）。請告知使用者。"


def tool_record_visual_assessment(crop_name: str, stage: str, vigor: int, coverage: int, health_note: str) -> str:
    """看完使用者上傳的「作物照片」後呼叫此工具，登記一筆結構化視覺評估，存入視覺生長日誌以長期追蹤。crop_name=照片中的作物（由圖說/你的辨識/當前焦點作物判定）；stage=你從外觀判定的生長階段（用：初期（幼苗）/發育期（旺盛生長）/中期（滿冠）/後期（成熟/採收） 其一）；vigor=生長勢 1~5（1萎黃弱、5濃綠旺）；coverage=冠層覆蓋度 1~5（1零星裸露、5完全覆蓋）；health_note=一句話健康/病蟲害觀察。系統會自動把你的視覺階段與 GDD 積溫推估的階段交叉檢核（照片是現場真相、GDD 是模型預測），並把檢核結論回傳給你——若兩者背離（照片落後或超前），請在回覆中向使用者點出並研判原因。即時寫入、無需確認。只在「確實收到作物照片」時呼叫一次。"""
    matched = match_crop_key(str(crop_name).strip()[:40]) if crop_name else "未指定作物"
    rec = record_visual_assessment(matched, stage, vigor, coverage, health_note)
    # 與 GDD 推估階段交叉檢核
    state = load_state()
    cd = state.get("crops", {}).get(matched, {})
    gdd_stage = None
    if cd:
        _, info = lookup_crop_info(matched)
        target = info.get("target_gdd", 1000.0)
        frac = (cd.get("accumulated_gdd", 0.0) / target) if target else 0.0
        gdd_stage = growth_stage(frac)
    cc = crosscheck_stage(rec["stage"], gdd_stage)
    base = (f"✅ 已登記視覺評估：{matched} — {rec['stage'] or '階段未判'}，"
            f"生長勢 {rec['vigor']}/5、覆蓋度 {rec['coverage']}/5。")
    if cc["verdict"] == "unknown":
        return base + "（此作物尚無 GDD 進度或階段無法判讀，本次未做交叉檢核。）"
    if cc["verdict"] == "aligned":
        return base + f"\n🔎 交叉檢核：視覺現況與 GDD 積溫推估（{cc['gdd']}）一致，模型可信。"
    flag = "落後於" if cc["verdict"] == "behind" else "超前於"
    return base + (f"\n🔎 交叉檢核：照片顯示作物**{flag}**GDD 積溫推估（視覺 {cc['visual']} vs 模型 {cc['gdd']}）。"
                   f"{cc['note']} 請在回覆中向使用者點出此落差並研判原因。")


def tool_query_visual_history(crop_name: str = "") -> str:
    """查詢過往照片的視覺生長評估履歷（階段、生長勢、覆蓋度、健康觀察隨時間的變化），用於看出某作物的視覺成長趨勢、或與 GDD 進度對照。crop_name 可留空查全部作物、或指定某作物。即時讀取本地資料庫，無耗時。"""
    return load_visual_history(crop_name=(match_crop_key(crop_name.strip()) if crop_name and crop_name.strip() else None), n=8)


def tool_record_prediction(metric: str, predicted_value: float, target_date: str, note: str) -> str:
    """登記一筆可驗證的量化預測，系統將於 target_date 過後自動以當日實測平均值驗證並回饋誤差給你。metric 必須是 air_temperature/air_humidity/soil_temperature/soil_humidity/soil_ec 之一；predicted_value 為你預測的當日平均值；target_date 格式 YYYY-MM-DD（今天起14天內）；note 為一句話的預測依據（100字內）。每當你做出量化判斷（如「明天土壤濕度約降至35%」）時呼叫。"""
    return save_prediction(metric, predicted_value, target_date, note)


def tool_query_prediction_history() -> str:
    """查詢你過往預測的校驗履歷（預測值 vs 實測值與誤差），用於檢視自己對這塊耕地的判斷偏差並進行校正。即時讀取本地檔案，無耗時。"""
    return load_prediction_feedback(n=12)


def tool_query_harvest_cadence() -> str:
    """查詢目前作物的割收節律歸納：累計割收次數、日曆週期（約幾天割一次，分近期與全期）、積溫週期（每輪約累積多少 ℃-day）、以及依節律推估的下次可割日。適用於可連續採收的作物（如空心菜）。資料來自使用者以 /harvest 指令登記的每次割收。即時讀取本地資料庫，無耗時。"""
    return get_harvest_cadence_summary()


def tool_get_et0_evapotranspiration() -> str:
    """取得耕地所在地（文山站博嘉國小）的即時氣象實測，並以 FAO-56 Penman-Monteith 公式推算今日參考作物蒸散量 ET₀（單位 mm/日），附帶當日降水與粗略水分收支。ET₀ 反映「作物今天大約蒸散掉多少水」，與土壤濕度實測互補：土濕看「現在有多濕」，ET₀ 看「水分流失速率」，兩者結合可做更精準的灌溉判斷。回傳值會標注數據成色（哪些經推估），請勿當成精確量測。需透過 CWA API 連線，耗時數秒。"""
    return get_et0_report()


def tool_record_harvest_event(crop_hint: str = "", note: str = "") -> str:
    """當使用者在對話中表達『他剛剛或今天採收／收成／割收了作物』時呼叫此工具，登記一次收成事件以更新採收週期統計。crop_hint 為使用者提到的作物（可留空，預設用當前設定作物）；note 為簡短備註（如數量描述，可留空）。注意：本工具不會立即寫入，而是發起一則確認，等待使用者下一句確認後才正式記錄。請在你的回覆中告訴使用者你將為他登記這次收成、並說明若只是隨口提及可以喊停。切勿在使用者只是『考慮』『詢問』『提到他人』收成時呼叫。"""
    if _current_chat_ctx.get() is None:
        return "（無法登記：缺少對話內容）"
    note_full = (f"{crop_hint} {note}".strip())[:100]
    ok = _set_pending_event(_current_chat_ctx.get(), "harvest", note_full)
    if not ok:
        return "目前已有另一筆尚未確認的事件，請先請使用者回覆前一個確認，再處理這次收成登記。"
    return "已發起收成登記的確認（若使用者於 10 分鐘內未回覆取消，系統將自動完成登記）。請在回覆中告知使用者你將為他登記這次收成，若只是隨口提及可回覆『不用』取消。"


def tool_record_fertilizer_event(note: str = "") -> str:
    """當使用者在對話中表達『他剛剛或今天施肥／追肥／施了某種肥』時呼叫此工具，登記一次施肥事件。note 為使用者描述（如『施了氮肥』『撒了有機肥』，可留空）。注意：本工具不會立即寫入，而是發起一則確認，等待使用者下一句確認後才正式記錄。請在你的回覆中告訴使用者你將為他登記這次施肥、並說明若只是隨口提及可以喊停。切勿在使用者只是『考慮』『詢問』施肥時呼叫。"""
    if _current_chat_ctx.get() is None:
        return "（無法登記：缺少對話內容）"
    ok = _set_pending_event(_current_chat_ctx.get(), "fertilizer", (note or "使用者登記施肥")[:100])
    if not ok:
        return "目前已有另一筆尚未確認的事件，請先請使用者回覆前一個確認，再處理這次施肥登記。"
    return "已發起施肥登記的確認（若使用者於 10 分鐘內未回覆取消，系統將自動完成登記）。請在回覆中告知使用者你將為他登記這次施肥，若只是隨口提及可回覆『不用』取消。"


def tool_search_agri_knowledge(query: str) -> str:
    """搜尋內建的台灣農業部官方出版品知識庫（百餘本栽培管理、病蟲害防治、土壤肥料、有機栽培技術手冊的全文）。query 為 2~10 字的關鍵詞，可用空格分隔多個詞（例：「空心菜 病害」「番茄 整枝」「液肥 稀釋」）。本地即時檢索、無耗時零成本。回傳最相關的原文段落與出處（書名＋頁碼）；引用其內容回覆使用者時，請註明書名。回答栽培方法、病蟲害診斷、施肥配方等知識性問題時，建議先查詢本知識庫以官方資料佐證，查無結果再依你的通用知識回答並註明未查得官方文獻。"""
    return search_knowledge(query)


def tool_assess_disease_risk() -> str:
    """評估目前田區的『葉部病害風險（病害壓力）』。系統會結合即時/近期的空氣濕度、氣溫，以及近 24 小時的高濕（葉面持續潮濕）時數，依植物病理經驗推算露菌病、白銹病、灰黴病、軟腐病、炭疽病等葉部病害的發生壓力，回傳風險等級（低/中/高）、觸發原因與較需留意的病害。當使用者詢問病害風險、近期濕度悶熱是否容易生病、是否該預防性防治，或你在分析中發現持續高濕悶熱時，呼叫此工具取得量化研判。即時讀取本地數據、無耗時。注意：這是『基於氣象條件的風險指標、非診斷』。風險達中/高時，回傳內容已自動附上候選病害的『官方防治參考』（農業部出版品節錄），請直接據此結合現場給預防建議並引用書名，不必再另查；若覺資訊不足再補呼叫 tool_search_agri_knowledge。務必說明這是尚未見病斑的『預防』而非確診用藥。"""
    return build_disease_report()


AGENT_TOOLS = [
    tool_get_realtime_sensor_data,
    tool_get_weather_forecast,
    tool_query_recent_history,
    tool_query_daily_summaries,
    tool_get_garden_status,
    tool_set_thresholds,
    tool_set_crop,
    tool_track_crop,
    tool_finish_crop,
    tool_record_prediction,
    tool_record_visual_assessment,
    tool_query_visual_history,
    tool_query_prediction_history,
    tool_query_harvest_cadence,
    tool_get_et0_evapotranspiration,
    tool_assess_disease_risk,
    tool_search_agri_knowledge,
    tool_record_harvest_event,
    tool_record_fertilizer_event,
]
