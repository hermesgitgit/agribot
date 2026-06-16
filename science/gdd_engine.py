# ======================================================================
# GDD 積溫引擎 — 結算編排 (GDD Engine: 逐日結算與多日回補)
# ======================================================================
# 本模組是 science 層中明確的「I/O 編排」：讀 temp_log / 感測歷史 / state，
# 呼叫 science/gdd.py 的純計算核心，再交易性寫回累計積溫。
import datetime
import re

from config import TZ_TAIPEI
from logging_setup import logger
from science.gdd import (
    DEFAULT_CROP_KEY,
    GDD_BACKFILL_MAX_DAYS,
    gdd_from_minmax,
    lookup_crop_info,
)
from storage.history import query_history_records
from storage.predictions import evaluate_due_predictions
from storage.state import active_crops, load_state, update_state
from storage.summaries import save_daily_summary_for_date
from storage.temp_log import get_temps_for_date


def day_minmax(target_date_str: str):
    """
    取某日的 (t_max, t_min, source)——與作物無關（多作物共用同一份每日溫度）。
    優先用 temp_log 高解析度日誌，不足則退回 sensor_history 事件式取樣；
    兩來源皆無時回 (None, None, 缺數據說明)。缺數據日絕不以假值估算。
    """
    temps = get_temps_for_date(target_date_str)
    source = f"temp_log 高解析度日誌 ({len(temps)} 點)"

    if len(temps) < 2:
        history_temps = []
        try:
            for record in query_history_records(date_str=target_date_str):
                temp_str = record.get("air_temperature", "無資訊")
                match = re.search(r'([0-9\.]+)', temp_str)
                if match:
                    history_temps.append(float(match.group(1)))
        except Exception as e:
            logger.warning(f"⚠️ [GDD Engine] 讀取感測歷史進行 GDD 計算時出錯: {e}")
        if len(history_temps) > len(temps):
            temps = history_temps
            source = "sensor_history (事件式取樣備援)"

    if len(temps) >= 2:
        return max(temps), min(temps), source
    if len(temps) == 1:
        return temps[0], temps[0], source + " (僅單筆數據)"
    logger.warning(f"⚠️ [GDD Engine] temp_log 與 history.json 中皆找不到 {target_date_str} 的溫度記錄，該日 GDD 將標記為缺數據跳過。")
    return None, None, "缺數據 (temp_log 與 history.json 皆無該日記錄)"


def calculate_daily_gdd(target_date_str: str, crop_name: str = None) -> dict:
    """
    計算特定日期、特定作物的 GDD（生長積溫）。crop_name 省略時取當前焦點作物。
    溫度來源與作物無關（day_minmax），僅作物的基溫/生長上限不同——故多作物只是
    對同一份當日溫度套用各自的公式。兩來源皆無數據時回傳 gdd=None（缺數據日不估算）。
    """
    if crop_name is None:
        crop_name = load_state().get("crop_name", "番茄 (Tomato)")
    crop_name, crop_info = lookup_crop_info(crop_name)
    t_base = crop_info["t_base"]
    t_upper = crop_info.get("t_upper", 30.0)

    t_max, t_min, source = day_minmax(target_date_str)
    if t_max is None:
        return {"crop_name": crop_name, "t_base": t_base, "t_upper": t_upper,
                "t_max": None, "t_min": None, "gdd": None, "source": source}

    return {
        "crop_name": crop_name,
        "t_base": t_base,
        "t_upper": t_upper,
        "t_max": t_max,
        "t_min": t_min,
        "gdd": gdd_from_minmax(t_min, t_max, t_base, t_upper),
        "source": source,
    }


def _pending_days_for(last_gdd_date_str, yesterday):
    """
    依某作物的 last_gdd_date 算出待結算日清單與「超出回補上限被跳過的天數」。
    回傳 (pending_days_list[str], skipped_old_days)。
    """
    try:
        last_date = datetime.datetime.strptime(last_gdd_date_str or "", "%Y-%m-%d").date()
        start_date = last_date + datetime.timedelta(days=1)
    except ValueError:
        start_date = yesterday  # 首次結算/日期欄毀損：只結算昨天，不回溯未知歷史
    skipped = 0
    floor = yesterday - datetime.timedelta(days=GDD_BACKFILL_MAX_DAYS - 1)
    if start_date < floor:
        skipped = (floor - start_date).days
        start_date = floor
    days, d = [], start_date
    while d <= yesterday:
        days.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return days, skipped


def _settle_crop(crop_name, pending_days, skipped_old, yesterday_str, minmax_cache):
    """
    計算單一作物在其待結算區間的新增 GDD（純計算，不寫 state）。
    minmax_cache 跨作物共用每日溫度，避免同一天被多個作物重複查 DB。
    顯示一律用「使用者實際存的作物名」（crop_name），lookup_crop_info 只用來
    借用積溫參數——資料庫沒有的作物（如龍鬚菜、秋葵）借用預設參數但仍顯示真名，
    絕不把名字洗成「預設作物 (Default)」。
    回傳 dict：crop_disp/t_base/t_upper/target_gdd/uses_default/added/lines/missing/skipped。
    """
    matched_key, info = lookup_crop_info(crop_name)
    uses_default = (matched_key == DEFAULT_CROP_KEY)  # 借用預設參數（DB 查無此作物）
    t_base, t_upper = info["t_base"], info.get("t_upper", 30.0)
    target_gdd = info.get("target_gdd", 1000.0)
    added, lines, missing = 0.0, [], 0
    for d_str in pending_days:
        if d_str not in minmax_cache:
            minmax_cache[d_str] = day_minmax(d_str)
        t_max, t_min, _ = minmax_cache[d_str]
        if t_max is None:
            missing += 1
            lines.append(f"  - {d_str}: 無實測溫度記錄，誠實跳過不累加")
        else:
            g = gdd_from_minmax(t_min, t_max, t_base, t_upper)
            added = round(added + g, 2)
            lines.append(f"  - {d_str}: 最高 {t_max}℃ / 最低 {t_min}℃ → +{g} ℃-day")
    return {"crop_disp": crop_name, "t_base": t_base, "t_upper": t_upper,
            "target_gdd": target_gdd, "uses_default": uses_default,
            "added": added, "lines": lines, "missing": missing, "skipped": skipped_old}


async def check_and_update_gdd() -> str:
    """
    結算所有「在種作物」尚未結算日子的 GDD（多作物、多日回補）：
    每個作物各自從其 last_gdd_date 的次日逐日結算至昨天——各作物的進度獨立。
    同一塊地共用同一份每日溫度，僅各自的基溫/生長上限不同。
    回補上限 GDD_BACKFILL_MAX_DAYS 天，更早缺口誠實告知；缺數據日誠實跳過。
    回傳合併的積溫報告字串（每個有結算的作物一段）；全部已結算時回傳 None。
    """
    now = datetime.datetime.now(TZ_TAIPEI)
    yesterday = (now - datetime.timedelta(days=1)).date()
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    state = load_state()
    # 在種作物（active=True）。全部停止追蹤（如全園清空）時為空——此時不結算任何作物，
    # 但下方仍會補昨日日彙總與驗證到期預測（那些是全園層級、與作物無關）。
    crops = active_crops(state)

    # 各作物的待結算區間（進度獨立）
    plans = {}  # crop -> (pending_days, skipped_old)
    for crop in crops:
        cd = state.get("crops", {}).get(crop, {})
        plans[crop] = _pending_days_for(cd.get("last_gdd_date", ""), yesterday)

    # 日彙總與預測驗證對全園共用、只做一次：涵蓋所有作物待結算日的聯集。
    # 順序很重要——先回補日彙總，停機期間到期的預測才能被判為可驗證。
    union_days = sorted({d for days, _ in plans.values() for d in days})
    for d_str in (union_days or [yesterday_str]):
        try:
            save_daily_summary_for_date(d_str)
        except Exception as ds_err:
            logger.warning(f"⚠️ [Daily Summary] 結算 {d_str} 日彙總失敗: {ds_err}")
    try:
        evaluate_due_predictions()
    except Exception as pe_err:
        logger.warning(f"⚠️ [Prediction Engine] 驗證到期預測失敗: {pe_err}")

    if not crops:
        logger.info("ℹ️ [GDD Engine] 目前沒有在種作物（全部已停止追蹤），不結算 GDD；日彙總與預測驗證已照常進行。")
        return None
    if not any(days for days, _ in plans.values()):
        logger.info(f"ℹ️ [GDD Engine] 所有在種作物昨天的 GDD ({yesterday_str}) 皆已結算過，略過。")
        return None

    # 各作物純計算新增 GDD（共用每日溫度快取）
    minmax_cache = {}
    computed = {}  # crop -> settle dict
    for crop, (days, skipped) in plans.items():
        if days or skipped:
            computed[crop] = _settle_crop(crop, days, skipped, yesterday_str, minmax_cache)

    # 交易性更新：一次持鎖把所有有結算的作物寫回（原子、避免與其他更新路徑互相覆蓋）
    settle_crops = {c: r for c, r in computed.items() if plans[c][0]}  # 真有待結算日的才寫

    def _apply(st):
        out = {}
        crops_dict = st.setdefault("crops", {})
        for crop, r in settle_crops.items():
            cd = crops_dict.setdefault(crop, {"accumulated_gdd": 0.0, "last_gdd_date": "", "active": True})
            try:
                base = float(cd.get("accumulated_gdd", 0.0))
            except (TypeError, ValueError):
                base = 0.0
            cd["accumulated_gdd"] = round(base + r["added"], 2)
            cd["last_gdd_date"] = yesterday_str
            out[crop] = cd["accumulated_gdd"]
        return out

    new_gdds = update_state(_apply)

    # 組報告：每個有結算的作物一段
    sections = []
    for crop, r in settle_crops.items():
        new_gdd = new_gdds.get(crop, 0.0)
        target_gdd = r["target_gdd"]
        param_note = "（使用預設積溫參數）" if r["uses_default"] else ""
        notes = ""
        if r["missing"]:
            notes += f"\n  ⚠️ 其中 {r['missing']} 天無實測數據，已跳過不累加。"
        if r["skipped"]:
            notes += f"\n  ⚠️ 更早的 {r['skipped']} 天缺口已超出 {GDD_BACKFILL_MAX_DAYS} 天回補上限，未予結算。"
        congrats = ""
        if new_gdd >= target_gdd:
            congrats = f"\n  🎉 已達成熟目標積溫 ({target_gdd} ℃-day)！建議評估採收。"
        sections.append(
            f"🌾 {r['crop_disp']}{param_note}（基溫 {r['t_base']}℃ / 上限 {r['t_upper']}℃）\n"
            + "\n".join(r["lines"]) + "\n"
            f"  📈 本次新增 +{r['added']} ℃-day｜🎒 累計 {new_gdd} / {target_gdd} ℃-day{notes}{congrats}"
        )

    header = "📊 【GDD 生長積溫報告】"
    if len(settle_crops) > 1:
        header += f"（{len(settle_crops)} 種在種作物）"
    log_msg = header + "\n\n" + "\n\n".join(sections)
    logger.info(f"✅ [GDD Engine] 結算完成:\n{log_msg}")
    return log_msg
