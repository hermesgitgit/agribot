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
# 割收記錄持久化 (Harvest Records)
# ======================================================================
# harvest_records 表的建表、寫入與查詢；節律歸納的純邏輯在 science/cadence.py。
import datetime
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from science.cadence import cadence_brief, cadence_summary_lines, maturity_calibration
from science.gdd import lookup_crop_info
from storage.common import STATE_FILE_LOCK
from storage.state import crop_target_gdd, find_tracked_crop, load_state


def _resolve_crop(state, crop_hint: str = "") -> str:
    """把使用者提到的作物對應到 state 裡實際存在的作物鍵；對不上就用焦點作物。

    比對用 find_tracked_crop（以 state 現有的鍵為準），因為 state 裡的鍵可能是
    自訂名而非 GDD 資料庫的標準鍵——只用正規化結果去比會對不上。
    """
    focus = state.get("crop_name", "未設定作物")
    hint = (crop_hint or "").strip()
    if not hint:
        return focus
    return find_tracked_crop(state.get("crops", {}), hint) or focus

HARVEST_TABLE_READY = False


# 距上次割收超過這個天數，就不視為同一輪的再生間隔，而是新一輪的基準點。
# 空心菜這類多次採收的葉菜再生約 7~14 天，抓 45 天足以容納季節性放慢，
# 又能擋掉清園休耕、漏登記造成的超長空窗污染節律統計。
HARVEST_CYCLE_GAP_DAYS = 45


def _ensure_harvest_table():
    global HARVEST_TABLE_READY
    if HARVEST_TABLE_READY:
        return
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS harvest_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    harvest_date TEXT,
                    crop_name TEXT,
                    is_first_of_cycle INTEGER,
                    days_since_prev INTEGER,
                    gdd_since_prev REAL,
                    accumulated_gdd_snapshot REAL,
                    note TEXT
                )
            ''')
            # 2026-10-29 新增：這次割收屬於哪一輪（/sow 的起算日）。舊表沒有這欄就補上；
            # 舊記錄留空，首採積溫校準只採計有起算日的記錄（累計值才有意義）。
            cols = {r[1] for r in conn.execute("PRAGMA table_info(harvest_records)").fetchall()}
            if "cycle_start_date" not in cols:
                conn.execute("ALTER TABLE harvest_records ADD COLUMN cycle_start_date TEXT")
            conn.commit()
            conn.close()
        HARVEST_TABLE_READY = True
    except Exception as e:
        logger.error(f"❌ [Harvest] 建立 harvest_records 表失敗: {e}")


def _current_accumulated_gdd(state, crop_name) -> float:
    return float(state.get("crops", {}).get(crop_name, {}).get("accumulated_gdd", 0.0))


def record_harvest(note: str = "", crop_hint: str = "") -> str:
    """
    登記一次割收事件。自動計算與「同作物上一次割收」的間隔天數與期間累積 GDD；
    若是該作物切換後的首次割收，標記為建立期基準點。回傳給使用者的結果訊息。

    crop_hint：使用者明講的作物（如「今天把空心菜割了」）。過去這個提示只被塞進
    備註字串，割收一律記到「焦點作物」頭上——同時種番茄與空心菜時，割空心菜會
    被記成割番茄，連 gdd_since_prev 都用番茄的積溫算，之後的節律統計就是不存在
    的數字。現在會先比對追蹤中的作物，比對不到才退回焦點作物。
    """
    _ensure_harvest_table()
    now = datetime.datetime.now(TZ_TAIPEI)
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    crop_name = _resolve_crop(state, crop_hint)
    acc_gdd = _current_accumulated_gdd(state, crop_name)
    crop_entry = state.get("crops", {}).get(crop_name, {})
    cycle_start = str(crop_entry.get("cycle_start_date", "") or "") if isinstance(crop_entry, dict) else ""

    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            # 取同作物的上一次割收
            prev = conn.execute(
                "SELECT harvest_date, accumulated_gdd_snapshot FROM harvest_records "
                "WHERE crop_name = ? ORDER BY harvest_date DESC, id DESC LIMIT 1",
                (crop_name,)).fetchone()
            # 本輪（同一個起算日）是否已經採收過：沒有 → 這次就是「首採」，累計 GDD 是成熟積溫樣本
            first_in_cycle = False
            if cycle_start:
                first_in_cycle = conn.execute(
                    "SELECT COUNT(*) FROM harvest_records WHERE crop_name = ? AND cycle_start_date = ?",
                    (crop_name, cycle_start)).fetchone()[0] == 0

            # 同日重複登記防呆：手滑連按兩次 /harvest（或確認逾時自動落檔後
            # 又手動補登）會寫入「間隔 0 天」的再生週期，把平均週期統計往下拉。
            if prev is not None and prev[0] == today_str:
                conn.close()
                return (f"⚠️ 今天（{today_str}）已登記過一次 {crop_name} 的割收，"
                        f"為避免 0 天間隔污染週期統計，本次不重複登記。")

            if prev is None:
                is_first = 1
                days_since = None
                gdd_since = None
            else:
                is_first = 0
                try:
                    prev_date = datetime.datetime.strptime(prev[0], "%Y-%m-%d")
                    days_since = (datetime.datetime.strptime(today_str, "%Y-%m-%d") - prev_date).days
                except Exception:
                    days_since = None
                gdd_since = round(acc_gdd - float(prev[1]), 2) if prev[1] is not None else None
                # 休耕空窗不是一次再生週期：清園後隔兩個月再種回同一作物，
                # 或單純漏登記幾輪，都會產生一個超長間隔。把它照樣寫成再生週期，
                # 會讓 cadence 的「近三次」變成 [9, 9, 120]、平均值失真且永久留存。
                # 超過門檻就視為新一輪的基準點（不計間隔），與作物切換的首次割收同等處理。
                if days_since is not None and days_since > HARVEST_CYCLE_GAP_DAYS:
                    logger.info(
                        f"🌱 [Harvest] {crop_name} 距上次割收 {days_since} 天，"
                        f"超過 {HARVEST_CYCLE_GAP_DAYS} 天視為新一輪的基準點，本次不計再生間隔。")
                    is_first = 1
                    days_since = None
                    gdd_since = None

            conn.execute(
                "INSERT INTO harvest_records "
                "(harvest_date, crop_name, is_first_of_cycle, days_since_prev, gdd_since_prev, "
                "accumulated_gdd_snapshot, note, cycle_start_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (today_str, crop_name, is_first, days_since, gdd_since, acc_gdd, str(note)[:100], cycle_start))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"❌ [Harvest] 寫入割收記錄失敗: {e}")
        return f"❌ 割收記錄失敗：{e}"

    logger.info(f"🌾 [Harvest] 已登記割收: {crop_name} @ {today_str}, 距上次 {days_since} 天, 期間 GDD {gdd_since}, 本輪起算 {cycle_start or '無'}")

    # 首採積溫：有 /sow 起算日、且本輪第一次採 → 這個累計值就是一筆「真實成熟積溫」
    maturity_text = ""
    if first_in_cycle:
        _, info = lookup_crop_info(crop_name)
        target = crop_target_gdd(state, crop_name, info)
        samples = _maturity_samples(crop_name)
        cal_text, _ = maturity_calibration(samples, target, crop_name)
        maturity_text = (f"\n🌱 本輪自 {cycle_start} 播種起累計 {acc_gdd} ℃-day 首採（目前目標 {target}）。"
                         + (f"\n{cal_text}" if cal_text else ""))
    elif not cycle_start:
        maturity_text = "\n（提示：播種／定植當天用 /sow 作物名 起算，之後的首採積溫才能用來校準目標。）"

    if is_first:
        return (f"🌾 已記錄 {crop_name} 的割收（{today_str}）。\n"
                f"這是本輪種植的第一次割收，將作為週期計時的基準點——"
                f"下次割收後我就能算出第一個再生週期了。{maturity_text}")
    else:
        days_text = f"{days_since} 天" if days_since is not None else "未知"
        gdd_text = f"，期間累積約 {gdd_since} ℃-day" if gdd_since is not None else ""
        days_list, gdds_list = _collect_regen_intervals(crop_name)
        return (f"🌾 已記錄 {crop_name} 的割收（{today_str}）。\n"
                f"距離上次割收 {days_text}{gdd_text}。\n"
                f"{cadence_brief(days_list, gdds_list)}{maturity_text}")


def _maturity_samples(crop_name):
    """各輪（依 cycle_start_date 分組）首次採收時的累計 GDD，由舊到新。只採計有起算日的記錄。"""
    _ensure_harvest_table()
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute(
                "SELECT cycle_start_date, accumulated_gdd_snapshot FROM harvest_records "
                "WHERE crop_name = ? AND cycle_start_date IS NOT NULL AND cycle_start_date != '' "
                "ORDER BY harvest_date, id", (crop_name,)).fetchall()
            conn.close()
        first_by_cycle = {}
        for start, snap in rows:
            if start not in first_by_cycle and snap is not None:
                first_by_cycle[start] = float(snap)
        return [first_by_cycle[k] for k in sorted(first_by_cycle)]
    except Exception as e:
        logger.warning(f"⚠️ [Harvest] 讀取首採積溫樣本失敗: {e}")
        return []


def _collect_regen_intervals(crop_name):
    """取得某作物所有再生期割收的 (間隔天數列表, 期間GDD列表)。"""
    _ensure_harvest_table()
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute(
                "SELECT days_since_prev, gdd_since_prev FROM harvest_records "
                "WHERE crop_name = ? AND is_first_of_cycle = 0 ORDER BY harvest_date, id",
                (crop_name,)).fetchall()
            conn.close()
        days = [r[0] for r in rows if r[0] is not None]
        gdds = [r[1] for r in rows if r[1] is not None]
        return days, gdds
    except Exception as e:
        logger.warning(f"⚠️ [Harvest] 讀取再生間隔失敗: {e}")
        return [], []


def get_harvest_cadence_summary(crop_name=None) -> str:
    """完整的割收節律摘要，供 /harvest_stats 指令與注入 AI prompt 使用。"""
    _ensure_harvest_table()
    if crop_name is None:
        crop_name = load_state().get("crop_name", "未設定作物")
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            total = conn.execute("SELECT COUNT(*) FROM harvest_records WHERE crop_name = ?", (crop_name,)).fetchone()[0]
            first = conn.execute(
                "SELECT harvest_date FROM harvest_records WHERE crop_name = ? AND is_first_of_cycle = 1 "
                "ORDER BY harvest_date LIMIT 1", (crop_name,)).fetchone()
            last = conn.execute(
                "SELECT harvest_date FROM harvest_records WHERE crop_name = ? ORDER BY harvest_date DESC LIMIT 1",
                (crop_name,)).fetchone()
            conn.close()
    except Exception as e:
        return f"【割收節律】讀取失敗：{e}"

    days, gdds = _collect_regen_intervals(crop_name)
    text = cadence_summary_lines(crop_name, total, first, last, days, gdds)
    # 成熟目標校準：各輪首採積溫 vs 目前目標
    state = load_state()
    entry = state.get("crops", {}).get(crop_name, {})
    _, info = lookup_crop_info(crop_name)
    target = crop_target_gdd(state, crop_name, info)
    src = "自訂" if isinstance(entry, dict) and entry.get("target_gdd") else "資料庫粗估"
    start = entry.get("cycle_start_date", "") if isinstance(entry, dict) else ""
    text += f"\n- 成熟目標積溫：{target} ℃-day（{src}）；本輪起算日：{start or '未用 /sow 起算'}"
    cal_text, _ = maturity_calibration(_maturity_samples(crop_name), target, crop_name)
    if cal_text:
        text += "\n- " + cal_text
    return text
