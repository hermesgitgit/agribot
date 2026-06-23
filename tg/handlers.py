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
# Telegram 訊息處理 (Handlers)
# ======================================================================
# 訊息分三路：本地指令（/status、/gdd、/harvest…，不經 AI 秒回零成本）、
# 待確認攔截（判定使用者是在確認還是取消上一筆收成/施肥事件）、其餘交給 AI。
# 照片會存檔並自動找 4–14 天前的舊照組成對照組做跨時生長診斷。
import asyncio
import io
import re

from PIL import Image

from agent.guard import (
    MAX_THRESHOLD_STEP, apply_crop_command, apply_threshold_command, strip_links,
)
from agent.pending import (
    classify_confirmation, clear_pending_event, commit_pending_event,
    get_pending_event, set_current_chat_context,
)
from agent.prompts import build_kb_context, build_state_summary, get_current_time_context
from agent.session import (
    get_chat_lock, get_or_create_chat, is_session_fresh, reset_chat, send_message_with_retry,
)
from config import HEARTBEAT_FILE, HEARTBEAT_URL, TELEGRAM_CHAT_ID, redact
from logging_setup import logger
from science.gdd import CROP_GDD_DATABASE, lookup_crop_info
from scrapers.cwa import get_et0_report
from storage.events import load_fertilizer_events
from storage.harvest import get_harvest_cadence_summary, record_harvest
from storage.history import query_history_records
from storage.knowledge import search_knowledge
from storage.photos import get_past_photo, save_photo
from storage.predictions import load_prediction_feedback
from storage.pushlog import load_last_push_brief
from storage.vision_log import load_visual_history
from storage.state import active_crops, load_state, set_crop_tracking, update_state
from tg.api import (
    BTN_FULL_ANALYSIS, BTN_SNAPSHOT, FARM_KEYBOARD, FARM_STATUS_QUESTION,
    download_telegram_photo, send_telegram_message, send_typing_action,
)
from watchdog import SCRAPER_FAILURE_ALERT_THRESHOLD, WATCHDOG_LOCK, WATCHDOG_STATE


async def handle_local_command(chat_id, text) -> bool:
    """
    處理本地輕量指令（/help, /status, /gdd, /threshold, /crop）。
    這些指令直接讀寫本地狀態檔，不經 Gemini、不啟動爬蟲——即時回覆、零 token 成本。
    回傳 True 表示已處理完畢；回傳 False 表示非已知指令，交由 AI 接手。
    """
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/help":
        await send_telegram_message(chat_id, (
            "📖 本地快速指令（即時回覆、不耗 AI 額度）：\n"
            "/status — 目前作物、生長階段、警戒門檻與最新感測讀數\n"
            "/gdd — 所有在種作物的生長積溫進度\n"
            "/crops — 作物追蹤清單（含已停止追蹤的）\n"
            "/crop_add 萵苣 — 加入一種同時在種的作物到每日 GDD 追蹤\n"
            "/crop_done 萵苣 — 停止累積某作物（清園後用，保留積溫）\n"
            "/harvest — 精確登記一次割收（亦可直接對我說「我收成空心菜了」）\n"
            "/harvest_stats — 查看割收週期歸納\n"
            "/et0 — 今日參考蒸散量與各作物需水 ETc（文山站實測推算）\n"
            "/vision — 照片視覺生長日誌（可加作物名，例：/vision 空心菜）\n"
            "/disease — 葉部病害風險評估（高濕悶熱 × 適病溫區，事前預警）\n"
            "/kb 關鍵詞 — 直接檢索農業部出版品知識庫（例：/kb 空心菜 病害）\n"
            "/health — 系統健康狀態（爬蟲成功率、心跳）\n"
            "/threshold dry=30 wet=80 — 手動設定土壤濕度警戒門檻（全園共用）\n"
            "/crop 空心菜 — 設定對話焦點作物\n"
            "/reset — 清空 AI 對話歷史\n"
            "👇 輸入框下方常駐兩顆快捷鍵：「🌱 耕地快照」＝秒回現況（同 /status）、"
            "「🔍 完整分析」＝即時爬取後做完整 AI 評估。\n"
            "💬 也可以直接自然地告訴我「我收成了」「我施肥了」或上傳收成照片，"
            "我會幫你登記（登記前會跟你確認一次）。"
        ), reply_markup=FARM_KEYBOARD)
        return True

    if cmd == "/health":
        with WATCHDOG_LOCK:
            agri = dict(WATCHDOG_STATE["agri"])
            cwa = dict(WATCHDOG_STATE["cwa"])
            cwa_obs = dict(WATCHDOG_STATE["cwa_obs"])
        def fmt_scraper(s):
            if s["consecutive_failures"] == 0:
                status = "🟢 正常"
            elif s["consecutive_failures"] < SCRAPER_FAILURE_ALERT_THRESHOLD:
                status = f"🟡 連續失敗 {s['consecutive_failures']} 次"
            else:
                status = f"🔴 連續失敗 {s['consecutive_failures']} 次"
            return f"{status}（上次成功：{s['last_success'] or '啟動後尚無'}）"
        ext = "已啟用" if HEARTBEAT_URL else "未設定（建議申請 healthchecks.io 並設 HEARTBEAT_URL 環境變數）"
        await send_telegram_message(chat_id, (
            f"🩺 系統健康狀態\n"
            f"🐢 阿龜爬蟲：{fmt_scraper(agri)}\n"
            f"🌤️ 氣象署爬蟲：{fmt_scraper(cwa)}\n"
            f"🔭 CWA 觀測 API（ET₀）：{fmt_scraper(cwa_obs)}\n"
            f"💓 心跳檔：每分鐘寫入 {HEARTBEAT_FILE}\n"
            f"🛰️ 外部死人開關：{ext}"
        ))
        return True

    if cmd == "/status":
        state = load_state()
        latest = "尚無歷史感測記錄"
        try:
            recent = query_history_records(limit=1)
            if recent:
                r = recent[-1]
                latest = (
                    f"{r.get('timestamp', '')}\n"
                    f"  🌡️ 氣溫 {r.get('air_temperature', '—')} / 💧 空氣濕度 {r.get('air_humidity', '—')}\n"
                    f"  🎚️ 土溫 {r.get('soil_temperature', '—')} / 🌊 土壤濕度 {r.get('soil_humidity', '—')} / 🧪 EC {r.get('soil_ec', '—')}"
                )
        except Exception as e:
            latest = f"讀取失敗: {e}"
        await send_telegram_message(chat_id, (
            f"📋 目前農園狀態\n"
            f"🌾 作物：{state.get('crop_name', '未設定')}\n"
            f"🌱 生長階段：{state.get('lifecycle', '未設定')}\n"
            f"🚰 警戒門檻：乾燥 < {state.get('dry_threshold', 30.0)}% / 積水 > {state.get('wet_threshold', 80.0)}%\n"
            f"📡 最新感測讀數：{latest}"
        ))
        return True

    if cmd == "/harvest":
        note = " ".join(parts[1:]).strip()
        msg = await asyncio.to_thread(record_harvest, note)
        await send_telegram_message(chat_id, msg)
        return True

    if cmd in ("/harvest_stats", "/harveststats", "/cadence"):
        summary = await asyncio.to_thread(get_harvest_cadence_summary)
        await send_telegram_message(chat_id, summary)
        return True

    if cmd == "/kb":
        keywords = " ".join(parts[1:]).strip()
        if not keywords:
            await send_telegram_message(chat_id, "用法：/kb 關鍵詞（例：/kb 空心菜 病害）")
            return True
        result = await asyncio.to_thread(search_knowledge, keywords)
        await send_telegram_message(chat_id, result)
        return True

    if cmd in ("/vision", "/photo_log", "/photolog"):
        crop = " ".join(parts[1:]).strip()
        from science.gdd import match_crop_key
        report = await asyncio.to_thread(load_visual_history, match_crop_key(crop) if crop else None, 10)
        await send_telegram_message(chat_id, report)
        return True

    if cmd in ("/et0", "/eto"):
        report = await asyncio.to_thread(get_et0_report)
        await send_telegram_message(chat_id, report)
        return True

    if cmd in ("/disease", "/risk"):
        from agent.prompts import build_disease_report
        report = await asyncio.to_thread(build_disease_report)
        await send_telegram_message(chat_id, report)
        return True

    if cmd == "/gdd":
        state = load_state()
        focus = state.get("crop_name", "未設定")
        tracked = active_crops(state)
        if not tracked:
            await send_telegram_message(chat_id, "📊 目前沒有在種作物（皆已停止追蹤）。\n用 /crop 作物名 設定焦點作物、或 /crop_add 作物名 加入追蹤；/crops 看含已停止的完整清單。")
            return True
        lines = ["📊 GDD 生長積溫進度（在種作物）"]
        for crop in tracked:
            cd = state.get("crops", {}).get(crop, {})
            acc = cd.get("accumulated_gdd", 0.0)
            _, info = lookup_crop_info(crop)
            target = info.get("target_gdd", 1000.0)
            pct = round(acc / target * 100.0, 1) if target else 0.0
            star = "⭐ " if crop == focus else "　 "
            lines.append(
                f"{star}{crop}\n"
                f"　 🎒 {acc} / {target} ℃-day（{pct}%）"
                f"｜基溫 {info.get('t_base')}℃/上限 {info.get('t_upper', 30.0)}℃"
                f"｜上次結算 {cd.get('last_gdd_date') or '無'}"
            )
        lines.append("（⭐ 為對話焦點作物；/crops 看全部含已停止追蹤的）")
        await send_telegram_message(chat_id, "\n".join(lines))
        return True

    if cmd == "/crops":
        state = load_state()
        focus = state.get("crop_name", "未設定")
        crops = state.get("crops", {})
        if not crops:
            await send_telegram_message(chat_id, "目前尚未設定任何作物。用 /crop 作物名 設定。")
            return True
        lines = ["🌱 作物追蹤清單"]
        for crop, cd in crops.items():
            active = cd.get("active", True)
            tag = "🟢 追蹤中" if active else "⚪ 已停止"
            star = "⭐" if crop == focus else "　"
            lines.append(f"{star}{tag}｜{crop}：累計 {cd.get('accumulated_gdd', 0.0)} ℃-day")
        lines.append("\n指令：/crop_add 作物名（加入每日追蹤）、/crop_done 作物名（停止累積、保留積溫）、/crop 作物名（設為焦點）")
        await send_telegram_message(chat_id, "\n".join(lines))
        return True

    if cmd in ("/crop_add", "/crop_done"):
        if len(parts) < 2:
            crops_list = "、".join(k for k in CROP_GDD_DATABASE.keys() if "預設" not in k)
            await send_telegram_message(chat_id, f"用法：{cmd} 作物名稱\n內建作物：{crops_list}")
            return True
        target_crop = " ".join(parts[1:]).strip()[:40]
        activate = (cmd == "/crop_add")
        matched, acc = await asyncio.to_thread(set_crop_tracking, target_crop, activate)
        if activate:
            await send_telegram_message(chat_id, f"🟢 已將「{matched}」加入每日 GDD 追蹤（目前累計 {acc} ℃-day），今晚 00:05 起一併結算。")
        elif acc is None:
            await send_telegram_message(chat_id, f"⚠️ 找不到作物「{matched}」，無法停止追蹤。用 /crops 查看清單。")
        else:
            await send_telegram_message(chat_id, f"⚪ 已停止追蹤「{matched}」（保留累計 {acc} ℃-day，不再每日累加）。")
        return True

    if cmd == "/threshold":
        arg_text = " ".join(parts[1:])
        dry_m = re.search(r'dry\s*=\s*([0-9\.]+)', arg_text)
        wet_m = re.search(r'wet\s*=\s*([0-9\.]+)', arg_text)
        if not (dry_m and wet_m):
            await send_telegram_message(chat_id, "用法：/threshold dry=30 wet=80")
            return True
        try:
            new_dry = float(dry_m.group(1))
            new_wet = float(wet_m.group(1))
        except ValueError:
            await send_telegram_message(chat_id, "⚠️ 數值格式錯誤。用法：/threshold dry=30 wet=80")
            return True
        if not (0.0 < new_dry < new_wet < 100.0):
            await send_telegram_message(chat_id, f"⚠️ 設定被拒絕：必須滿足 0 < dry < wet < 100（收到 dry={new_dry}, wet={new_wet}）。")
            return True
        def _apply(state):
            state["dry_threshold"] = new_dry
            state["wet_threshold"] = new_wet
        await asyncio.to_thread(update_state, _apply)
        await send_telegram_message(chat_id, (
            f"✅ 警戒門檻已手動更新：乾燥 < {new_dry}% / 積水 > {new_wet}%\n"
            f"（手動指令不受 AI 自主調整的單次 ±{MAX_THRESHOLD_STEP} 百分點限幅）"
        ))
        return True

    if cmd == "/crop":
        if len(parts) < 2:
            crops_list = "、".join(k for k in CROP_GDD_DATABASE.keys() if "預設" not in k)
            await send_telegram_message(chat_id, f"用法：/crop 作物名稱\n內建作物：{crops_list}")
            return True
        new_crop = " ".join(parts[1:]).strip()[:40]
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
            entry["active"] = True  # 設為焦點即視為在種、納入每日追蹤（與 tool_set_crop 一致）
            return entry.get("accumulated_gdd", 0.0)
        acc = await asyncio.to_thread(update_state, _apply)
        await send_telegram_message(chat_id, f"✅ 焦點作物已設為：{matched}（目前累計 GDD: {acc} ℃-day，並已納入每日追蹤）")
        return True

    return False  # 未知指令交由 AI 處理


async def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    photo = message.get("photo")
    caption = message.get("caption", "").strip()
    
    # 🔒 安全防護：只允許指定的擁有者進行對話。
    # 對未授權者「靜默忽略」而非回覆：回覆會確認 bot 存活、且讓任何人都能
    # 驅動本 bot 對外發訊（惡意刷訊息可消耗 Telegram API 額度、觸發平台限流，
    # 連帶延誤發給擁有者的正常警報）。僅記 log 供事後稽核。
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        logger.info(f"🔒 已靜默忽略未授權的對話請求。Chat ID: {chat_id}")
        return

    user_input_text = caption if photo else text

    # 輸出收到訊息日誌
    if photo:
        logger.info(f"📸 收到擁有者上傳的照片！附帶文字: {caption}")
    else:
        logger.info(f"📩 收到擁有者訊息: {text}")

    # 🌱 耕地狀況快捷按鍵（輸入框下方常駐 reply keyboard）
    if not photo and text == BTN_SNAPSHOT:
        # 耕地快照：直接走本地 /status，秒回、不爬新數據、零 AI 額度
        await handle_local_command(chat_id, "/status")
        return
    if not photo and text == BTN_FULL_ANALYSIS:
        # 完整分析：等同使用者輸入「現在耕地的狀況如何？」，往下交由 AI
        # 即時爬取後分析。改寫 text/user_input_text 後讓流程照常進行。
        text = FARM_STATUS_QUESTION
        user_input_text = FARM_STATUS_QUESTION

    # ⚙️ 待確認事件攔截：若上一輪 AI 發起了收成/施肥登記且尚在等待確認，
    # 這一則訊息優先當作「確認回覆」處理（預設會記、明確否定才取消）。
    pending = get_pending_event(chat_id)
    if pending and not text.startswith("/") and not (photo and caption.startswith("/")):
        verdict = classify_confirmation(user_input_text)
        ev_label = "收成" if pending["type"] == "harvest" else "施肥"
        if verdict == "no":
            clear_pending_event(chat_id)
            await send_telegram_message(chat_id, f"👌 好的，這次的{ev_label}就不記錄了。")
            return
        elif verdict == "yes":
            result = await asyncio.to_thread(commit_pending_event, chat_id)
            if result:
                await send_telegram_message(chat_id, result)
            else:
                await send_telegram_message(chat_id, f"⚠️ 沒有待登記的{ev_label}事件（可能已自動登記或已取消），本次未重複寫入。")
            return
        # verdict == unclear：不攔截，讓訊息照常進入 AI 對話；
        # 待確認事件保留——逾時後由 watchdog 視為默認同意自動落檔
    
    # 對話重置指令 (僅在無照片且文字是指令時觸發)
    if not photo and (text.startswith("/reset") or text.startswith("/clear") or text.startswith("/start")):
        # 一併清除殘留的待確認事件：使用者重置後通常認為一切歸零，
        # 不該讓 10 分鐘內的一句「好的」誤觸發一筆他已忘記的登記
        clear_pending_event(chat_id)
        reset_chat(chat_id)
        await send_telegram_message(
            chat_id,
            "🌱 歡迎使用智慧農務 Agentic Bot！已為您清空對話歷史，現在可以重新設定或詢問囉！\n"
            "例如：「我現在剛種了空心菜幼苗，今天需要澆水嗎？」\n"
            "👇 輸入框下方有兩顆快捷鍵：「🌱 耕地快照」秒回現況、「🔍 完整分析」做即時完整評估。\n"
            "輸入 /help 可查看本地快速指令（查狀態、查積溫、手動設門檻，即時回覆且不耗 AI 額度）。",
            reply_markup=FARM_KEYBOARD
        )
        return

    # 本地輕量指令：直接讀寫本地狀態，不經 Gemini、不啟動爬蟲，秒回且零 token 成本。
    # 照片附帶的 caption 指令同樣生效（例如傳收成照片並附「/harvest 第三次」），
    # 此時照片仍會存入相簿，不遺失影像記錄。
    command_text = text if (not photo and text.startswith("/")) else (
        caption if (photo and caption.startswith("/")) else "")
    if command_text:
        handled = await handle_local_command(chat_id, command_text)
        if handled:
            if photo:
                image_bytes = await download_telegram_photo(photo[-1]["file_id"])
                if image_bytes:
                    await asyncio.to_thread(save_photo, image_bytes)
            return

    # 發送 Typing 動作告知機器人正在思考
    await send_typing_action(chat_id)

    # 由於 Playwright 爬蟲需要一定時間，我們啟動一個 background task 持續發送 Typing 動作維持 Telegram 狀態
    keep_typing = True
    async def typing_loop():
        while keep_typing:
            await send_typing_action(chat_id)
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(typing_loop())
    # 在 try 外先初始化，確保 finally 區塊無論如何都能安全引用（避免 NameError）
    pil_image = None
    past_pil_image = None
    try:
        # 2. 生長相片存檔與雙圖檢索
        if photo:
            file_id = photo[-1]["file_id"]  # 取得最高解析度的照片
            image_bytes = await download_telegram_photo(file_id)
            if image_bytes:
                # 儲存當前照片到 NAS
                current_photo_path = save_photo(image_bytes)
                try:
                    pil_image = Image.open(io.BytesIO(image_bytes))
                    logger.info("🖼️ 成功下載最新照片並載入為 PIL Image。")
                    
                    # 嘗試抓取一週前的對照歷史照片
                    past_photo_path = get_past_photo(current_photo_path)
                    if past_photo_path:
                        try:
                            past_pil_image = Image.open(past_photo_path)
                            logger.info("🖼️ 成功載入歷史照片作為對照組。")
                        except Exception as past_err:
                            logger.warning(f"⚠️ 載入歷史照片失敗: {past_err}")
                except Exception as img_err:
                    logger.warning(f"⚠️ 解析當下圖片失敗: {img_err}")
            else:
                await send_telegram_message(chat_id, "⚠️ 圖片下載失敗，系統將僅根據數據和文字進行分析。")

        # ============ Agentic 模式 ============
        # 不再由程式以關鍵字預判是否爬取數據——模型將透過工具（即時感測、天氣、
        # 歷史、日彙總）自主決定需要哪些資訊、按需呼叫（ReAct 迴路）。
        # prompt 結構：使用者訊息置頂（模型必須先回應「這句話」的意圖），
        # 輕量背景（時間、狀態摘要、最近推播、施肥記錄、預測回饋）退居其後僅供參考
        # ——避免短的對話性訊息被農務報告鷹架淹沒而答非所問。
        # 必須在 get_or_create_chat 建立 session「之前」判斷，否則永遠是 False
        session_fresh = is_session_fresh(chat_id)

        time_context = get_current_time_context()
        fertilizer_summary = load_fertilizer_events()
        prediction_feedback = load_prediction_feedback()
        state_summary = build_state_summary()
        last_push_brief = load_last_push_brief()

        # 構建多模態 Prompt
        prompt_parts = []

        # 多模態雙圖注入 (歷史在前，當前在後)
        if past_pil_image:
            prompt_parts.append(past_pil_image)
        if pil_image:
            prompt_parts.append(pil_image)

        if photo:
            if past_pil_image:
                user_block = f"使用者上傳了照片（已附上兩張作物照片：第一張是歷史照片、第二張是最新照片），並附帶文字說明：{user_input_text if user_input_text else '請幫我對比生長狀況並診斷。'}"
            else:
                user_block = f"使用者上傳了當前最新照片，並附帶文字說明：{user_input_text if user_input_text else '請幫我診斷這張照片中的作物狀況並給予建議。'}"
            user_block += "\n（注意：若使用者的文字說明顯示這是一張『收成照片』、或提到他採收/割收了，請依準則 14 呼叫 tool_record_harvest_event 登記收成；若只是生長診斷或病蟲害照片，則正常診斷、不要登記。）"
            user_block += "\n（診斷後請依準則 7 呼叫 tool_record_visual_assessment 登記這次的結構化視覺評估，並依工具回傳的「視覺 vs GDD 交叉檢核」結論在回覆中點出落差。）"
            # 附上過往視覺日誌，讓 AI 看出長期趨勢
            vh = load_visual_history(n=6)
            if "尚無" not in vh:
                user_block += f"\n\n{vh}"
        else:
            user_block = f"使用者訊息：「{user_input_text}」"

        # 記憶重置提醒：重啟/每日輪替後的第一則訊息，提示模型誠實面對失憶
        fresh_note = ""
        if session_fresh:
            fresh_note = (
                "\n【提醒】你的對話記憶剛重置（系統重啟或每日輪替），這是新對話的第一則訊息。"
                "若使用者指涉先前的對話內容，請坦白說明記憶已重置並請他補充，切勿裝懂。"
            )

        # 病蟲害/栽培/防治類問題：系統先查知識庫、把官方節錄塞進背景，確保模型有書名可引
        kb_context = await asyncio.to_thread(build_kb_context, user_input_text)
        kb_block = ""
        if kb_context:
            kb_block = (
                "【系統為本題預先檢索的農業部官方文獻】\n"
                f"{kb_context}\n"
                "（回答病蟲害/栽培/防治時，請優先依據上列官方文獻、並在回覆中引用書名《》；"
                "資訊不足可再自行呼叫 tool_search_agri_knowledge。）\n\n"
            )

        full_prompt = (
            f"{user_block}\n"
            f"（請先判斷並直接回應上述訊息的意圖：若它是對話、澄清或糾正，自然簡短回應即可、"
            f"不要套用農務報告格式；若是農務問題，再參考以下背景與工具進行分析。）{fresh_note}\n\n"
            f"【系統背景資訊 - {time_context}】\n\n"
            f"{state_summary}\n\n"
            + (f"{last_push_brief}\n\n" if last_push_brief else "")
            + f"{fertilizer_summary}\n\n"
            f"{prediction_feedback}\n\n"
            + kb_block
            + f"（提示：如需即時感測數據、天氣預報、近期歷史或長期日彙總，請自主呼叫對應工具後再下結論。）"
        )

        prompt_parts.append(full_prompt)
        
        # 設定當前對話 context，讓 AI 的收成/施肥事件工具知道要登記給哪個 chat
        set_current_chat_context(chat_id)

        # 使用 Gemini 進行分析（帶有強健重試機制）。
        # 持對話鎖：序列化同一對話的 send_message（會話的取得/每日輪替
        # 也一併納入鎖內，避免並發訊息各自建立 session 互相覆蓋）。
        async with get_chat_lock(chat_id):
            chat = get_or_create_chat(chat_id)
            response = await asyncio.to_thread(send_message_with_retry, chat, prompt_parts)
        # 新 SDK 在「本輪無文字回應」時 .text 回 None（舊版是拋例外）——
        # 必須擋下，否則 None 流進後續清洗鏈會炸 TypeError。空回覆誠實告知。
        ai_message = (response.text or "").strip()
        if not ai_message:
            ai_message = "（這一輪我沒有成功產生文字回覆，請再說一次或換個說法試試。）"
        
        # 4. & 5. 閉環控制指令：統一經過 Command Guard 驗證後套用
        ai_message = apply_threshold_command(ai_message, source_tag="對話")
        ai_message = apply_crop_command(ai_message, source_tag="對話")
        ai_message = strip_links(ai_message, source_tag="對話")

        logger.info(f"🤖 Gemini 回覆:\n{ai_message}")
        await send_telegram_message(chat_id, ai_message)

    except Exception as e:
        logger.error(f"❌ 處理 Gemini 訊息失敗: {redact(e)}")
        await send_telegram_message(chat_id, f"❌ 系統分析時發生錯誤，請稍後再試。錯誤描述：{redact(e)}")
    finally:
        keep_typing = False
        typing_task.cancel()
        # 釋放 PIL Image 佔用的檔案描述符（長期運行避免累積洩漏）
        for _img in (pil_image, past_pil_image):
            if _img is not None:
                try:
                    _img.close()
                except Exception:
                    pass
