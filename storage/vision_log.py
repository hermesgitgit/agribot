# ======================================================================
# 視覺生長日誌持久化 (Visual Growth Log)
# ======================================================================
# 把使用者上傳照片時 AI 產出的「結構化視覺評估」按作物存檔，
# 形成可追蹤的視覺生長軌跡——從「閱後即焚的對話診斷」升級成長期監控資料。
# 與 GDD（熱量時間）互補：GDD 是模型預測、視覺是現場真相。
import datetime
import sqlite3

from config import DB_FILE, TZ_TAIPEI
from logging_setup import logger
from science.vision import COVERAGE_SCALE, VIGOR_SCALE, validate_score
from storage.common import STATE_FILE_LOCK

_TABLE_READY = False


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS vision_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    crop_name TEXT,
                    stage TEXT,
                    vigor INTEGER,
                    coverage INTEGER,
                    health_note TEXT,
                    photo_path TEXT
                )
            ''')
            conn.commit()
            conn.close()
        _TABLE_READY = True
    except Exception as e:
        logger.error(f"❌ [Vision Log] 建立 vision_log 表失敗: {e}")


def record_visual_assessment(crop_name, stage, vigor, coverage, health_note, photo_path="") -> dict:
    """
    登記一筆視覺評估（由 AI 看照片後產出）。vigor/coverage 為 1~5 分級（夾值）。
    回傳實際寫入的正規化內容（供回呼端組訊息）。失敗只記 log。
    """
    _ensure_table()
    ts = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M")
    v = validate_score(vigor, VIGOR_SCALE)
    c = validate_score(coverage, COVERAGE_SCALE)
    note = str(health_note or "")[:200]
    crop = str(crop_name or "未指定作物")[:40]
    stg = str(stage or "")[:40]
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            conn.execute(
                "INSERT INTO vision_log (ts, crop_name, stage, vigor, coverage, health_note, photo_path) "
                "VALUES (?,?,?,?,?,?,?)", (ts, crop, stg, v, c, note, str(photo_path or "")[:200]))
            conn.commit()
            conn.close()
        logger.info(f"📸 [Vision Log] 已登記視覺評估: {crop} / {stg} / 勢{v} 蓋{c} / {note[:30]}")
    except Exception as e:
        logger.error(f"❌ [Vision Log] 寫入失敗: {e}")
    return {"ts": ts, "crop_name": crop, "stage": stg, "vigor": v, "coverage": c, "health_note": note}


def load_visual_history(crop_name=None, n=8) -> str:
    """
    讀取最近 n 筆視覺評估（給定 crop_name 則只取該作物），格式化為文字供注入 prompt
    或 /vision 指令。呈現由舊到新以利看出趨勢；無記錄時誠實回報。
    """
    _ensure_table()
    try:
        with STATE_FILE_LOCK:
            conn = sqlite3.connect(DB_FILE)
            if crop_name:
                rows = conn.execute(
                    "SELECT ts, crop_name, stage, vigor, coverage, health_note FROM vision_log "
                    "WHERE crop_name = ? ORDER BY id DESC LIMIT ?", (str(crop_name)[:40], n)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, crop_name, stage, vigor, coverage, health_note FROM vision_log "
                    "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            conn.close()
    except Exception as e:
        return f"【視覺生長日誌】讀取失敗：{e}"

    if not rows:
        who = f"「{crop_name}」" if crop_name else ""
        return (f"【視覺生長日誌】{who}目前尚無照片評估記錄。"
                "上傳作物照片時，系統會自動登記階段與長勢，逐步累積視覺成長軌跡。")

    rows.reverse()  # 由舊到新
    scope = f"（{crop_name}）" if crop_name else "（全部作物）"
    lines = [f"【視覺生長日誌{scope}，最近 {len(rows)} 筆，由舊到新】",
             "（勢=生長勢 1~5，蓋=冠層覆蓋度 1~5；視覺為粗分級定性觀察、零星上傳非連續序列）"]
    for ts, crop, stage, vigor, coverage, note in rows:
        v = vigor if vigor is not None else "—"
        c = coverage if coverage is not None else "—"
        lines.append(f"- {ts} {crop}：{stage or '階段未判'}，勢{v}/蓋{c}"
                     + (f"，{note}" if note else ""))
    return "\n".join(lines)
