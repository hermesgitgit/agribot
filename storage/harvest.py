# ======================================================================
# 割收記錄持久化 (Harvest Records)
# ======================================================================
# harvest_records 表的建表、寫入與查詢；節律歸納的純邏輯在 science/cadence.py。
import datetime
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from science.cadence import cadence_brief, cadence_summary_lines
from storage.common import STATE_FILE_LOCK
from storage.state import load_state

HARVEST_TABLE_READY = False


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
            conn.commit()
            conn.close()
        HARVEST_TABLE_READY = True
    except Exception as e:
        logger.error(f"❌ [Harvest] 建立 harvest_records 表失敗: {e}")


def _current_accumulated_gdd(state, crop_name) -> float:
    return float(state.get("crops", {}).get(crop_name, {}).get("accumulated_gdd", 0.0))


def record_harvest(note: str = "") -> str:
    """
    登記一次割收事件。自動計算與「同作物上一次割收」的間隔天數與期間累積 GDD；
    若是該作物切換後的首次割收，標記為建立期基準點。回傳給使用者的結果訊息。
    """
    _ensure_harvest_table()
    now = datetime.datetime.now(TZ_TAIPEI)
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    crop_name = state.get("crop_name", "未設定作物")
    acc_gdd = _current_accumulated_gdd(state, crop_name)

    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            # 取同作物的上一次割收
            prev = conn.execute(
                "SELECT harvest_date, accumulated_gdd_snapshot FROM harvest_records "
                "WHERE crop_name = ? ORDER BY harvest_date DESC, id DESC LIMIT 1",
                (crop_name,)).fetchone()

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

            conn.execute(
                "INSERT INTO harvest_records "
                "(harvest_date, crop_name, is_first_of_cycle, days_since_prev, gdd_since_prev, accumulated_gdd_snapshot, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (today_str, crop_name, is_first, days_since, gdd_since, acc_gdd, str(note)[:100]))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"❌ [Harvest] 寫入割收記錄失敗: {e}")
        return f"❌ 割收記錄失敗：{e}"

    logger.info(f"🌾 [Harvest] 已登記割收: {crop_name} @ {today_str}, 距上次 {days_since} 天, 期間 GDD {gdd_since}")

    if is_first:
        return (f"🌾 已記錄 {crop_name} 的割收（{today_str}）。\n"
                f"這是本輪種植的第一次割收，將作為週期計時的基準點——"
                f"下次割收後我就能算出第一個再生週期了。")
    else:
        days_text = f"{days_since} 天" if days_since is not None else "未知"
        gdd_text = f"，期間累積約 {gdd_since} ℃-day" if gdd_since is not None else ""
        days_list, gdds_list = _collect_regen_intervals(crop_name)
        return (f"🌾 已記錄 {crop_name} 的割收（{today_str}）。\n"
                f"距離上次割收 {days_text}{gdd_text}。\n"
                f"{cadence_brief(days_list, gdds_list)}")


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
    return cadence_summary_lines(crop_name, total, first, last, days, gdds)
