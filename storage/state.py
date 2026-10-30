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
# 農園狀態檔 state.json：當前作物、生長階段、警戒門檻、各作物累計 GDD
# ======================================================================
import json
import os

from config import DEFAULT_DRY_THRESHOLD, DEFAULT_WET_THRESHOLD, STATE_FILE
from logging_setup import logger
from storage.common import STATE_FILE_LOCK, atomic_write_json


def load_state() -> dict:
    """
    從 state.json 讀取當前作物生長狀態與警戒閾值，以及各作物的 GDD 積溫狀態。
    若檔案不存在，則回傳預設的配置。
    """
    default_state = {
        "lifecycle": "seedling (幼苗期)",
        "dry_threshold": DEFAULT_DRY_THRESHOLD,
        "wet_threshold": DEFAULT_WET_THRESHOLD,
        "crop_name": "番茄 (Tomato)",
        "crops": {
            "番茄 (Tomato)": {
                "accumulated_gdd": 0.0,
                "last_gdd_date": ""
            }
        }
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with STATE_FILE_LOCK:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

        # 確保有 crop_name
        if "crop_name" not in state:
            state["crop_name"] = "番茄 (Tomato)"

        # 確保有 crops 物件且格式正確
        if "crops" not in state or not isinstance(state["crops"], dict):
            state["crops"] = {}

        # 確保當前選取的作物在 crops 字典中初始化
        active_crop = state["crop_name"]
        if active_crop not in state["crops"]:
            state["crops"][active_crop] = {
                "accumulated_gdd": 0.0,
                "last_gdd_date": ""
            }

        # 多作物 GDD 遷移：為「尚無 active 旗標」的作物補上預設值（只在首次升級時生效）。
        # 舊資料只把「當前焦點作物」標為在種、其餘標為歷史，確保升級後行為與升級前一致
        # （絕不讓早已切換掉的作物突然每日累積 GDD）。之後由 set_crop_tracking() / /crop /
        # tool_set_crop 顯式控制——這裡只用 setdefault，絕不每次載入都強制覆寫，
        # 否則使用者以 /crop_done 停止焦點作物（清園）的設定會被悄悄撤銷。
        for name, c in state["crops"].items():
            if isinstance(c, dict):
                c.setdefault("active", name == active_crop)

        return state
    except Exception as e:
        logger.warning(f"⚠️ 讀取 state.json 失敗: {e}")
        return default_state


def active_crops(state) -> list:
    """
    回傳目前「在種、要每日累積 GDD」的作物名稱清單——即 active=True 的作物。
    供 GDD 結算引擎逐一結算、報告逐一列示。
    注意：不再硬塞當前焦點作物——使用者以 /crop_done 明確停止焦點作物（清園）時，
    它就該真的停止；否則「停止」會被悄悄撤銷（焦點作物無限期累積的 bug）。
    焦點作物在正常設定（/crop、tool_set_crop）時都已被標為 active，故無需硬塞。
    """
    crops = state.get("crops", {})
    return [n for n, c in crops.items() if isinstance(c, dict) and c.get("active", True)]


def find_tracked_crop(crops, crop_name: str):
    """在「state 裡實際存在的作物鍵」中找出對應者；找不到回 None。

    為什麼不能只靠 match_crop_key：它把名稱正規化成 GDD 資料庫的標準鍵
    （'龍鬚菜' → '龍鬚菜 (Chayote Shoot)'），但 state 裡存的鍵是「當初登記
    那一刻」算出來的。若某作物是在被加進 CROP_GDD_DATABASE 之前就以自訂名
    註冊，state 裡會是原始的 '龍鬚菜'，之後所有查詢都用新標準鍵去找 → 永遠
    找不到：/crops 明明列著它，/crop_done 與 tool_finish_crop 卻回報查無此作物，
    而且停不掉、GDD 一直累加。反過來（先標準後自訂）也會產生重複帳。
    比對順序：完全相同 → 正規化後相同 → 首詞互含。
    """
    if not crops:
        return None
    raw = str(crop_name or "").strip()[:40]
    if not raw:
        return None
    if raw in crops:
        return raw
    from science.gdd import match_crop_key  # 延遲匯入避免 storage→science 載入期耦合
    canonical = match_crop_key(raw)
    if canonical in crops:
        return canonical
    for key in crops:
        if match_crop_key(str(key)) == canonical:
            return key
    first = raw.split()[0]
    for key in crops:                      # 最後才用首詞互含（與 match_crop_key 同風格）
        k = str(key)
        if first and (first in k or k.split()[0] in raw):
            return key
    return None


def set_crop_tracking(crop_name: str, active: bool):
    """
    將作物加入（active=True）或移出（active=False）每日 GDD 追蹤。
    加入時若作物尚不存在則初始化其積溫帳；停止時保留歷史積溫、僅不再累加。
    回傳 (實際使用的作物鍵, 該作物目前累計 GDD)；要停止一個不存在的作物時回 (name, None)。
    """
    from science.gdd import match_crop_key  # 延遲匯入避免 storage→science 載入期耦合
    canonical = match_crop_key(str(crop_name).strip()[:40])

    def _apply(state):
        crops = state.setdefault("crops", {})
        existing = find_tracked_crop(crops, crop_name)
        if active:
            # 已有帳就沿用既有鍵，不要因為正規化結果不同而開第二個帳
            key = existing or canonical
            entry = crops.get(key)
            if not isinstance(entry, dict):
                # 相容極舊格式（名稱 → 積溫數值）：升級成 dict 並保留積溫
                acc_old = float(entry) if isinstance(entry, (int, float)) else 0.0
                entry = {"accumulated_gdd": acc_old, "last_gdd_date": ""}
                crops[key] = entry
            entry["active"] = True
            return (key, entry.get("accumulated_gdd", 0.0))
        # 停止追蹤
        if existing is None:
            return (canonical, None)
        entry = crops[existing]
        if not isinstance(entry, dict):
            acc_old = float(entry) if isinstance(entry, (int, float)) else 0.0
            entry = {"accumulated_gdd": acc_old, "last_gdd_date": ""}
            crops[existing] = entry
        entry["active"] = False
        return (existing, entry.get("accumulated_gdd", 0.0))

    key, acc = update_state(_apply)
    return key, acc


def crop_target_gdd(state, crop_key, info=None) -> float:
    """
    某作物「目前生效」的成熟目標積溫：使用者用 /target（或實收校準後）設定的
    自訂值優先，否則用 CROP_GDD_DATABASE 的粗估值。所有讀目標的地方一律經此，
    否則 /gdd、推播、Kc 需水估算會各說各話。
    """
    entry = state.get("crops", {}).get(crop_key, {})
    if isinstance(entry, dict):
        try:
            custom = entry.get("target_gdd")
            if custom is not None and float(custom) > 0:
                return float(custom)
        except (TypeError, ValueError):
            pass
    if info is None:
        from science.gdd import lookup_crop_info  # 延遲匯入避免 storage→science 載入期耦合
        _, info = lookup_crop_info(crop_key)
    return float(info.get("target_gdd", 1000.0))


def start_crop_cycle(crop_name: str, note: str = "", today: str = None):
    """
    播種／定植：為某作物開始新的一輪——累計 GDD 歸零、記下 cycle_start_date、
    納入每日追蹤。這是「首採積溫校準」的前提：沒有起算點，累計值就只是從
    不知哪天起的總和，拿來當成熟積溫沒有意義。
    上一輪的累計與起算日存進 prev_cycle，誤按可查、可手動還原。
    回傳 (實際作物鍵, 上一輪累計 GDD, 上一輪起算日或 '')。
    """
    import datetime
    from config import TZ_TAIPEI
    from science.gdd import match_crop_key  # 延遲匯入避免 storage→science 載入期耦合
    today = today or datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    raw = str(crop_name or "").strip()[:40]
    canonical = match_crop_key(raw)

    def _apply(state):
        crops = state.setdefault("crops", {})
        key = find_tracked_crop(crops, raw) or canonical
        entry = crops.get(key)
        if not isinstance(entry, dict):
            acc_old = float(entry) if isinstance(entry, (int, float)) else 0.0
            entry = {"accumulated_gdd": acc_old, "last_gdd_date": ""}
            crops[key] = entry
        old_acc = float(entry.get("accumulated_gdd", 0.0) or 0.0)
        old_start = entry.get("cycle_start_date", "") or ""
        entry["prev_cycle"] = {"accumulated_gdd": old_acc, "cycle_start_date": old_start, "ended": today}
        entry["accumulated_gdd"] = 0.0
        # 從今天起算：昨天以前不補結算（last_gdd_date=昨天 → 引擎明晚只結算今天）
        yesterday = (datetime.datetime.strptime(today, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        entry["last_gdd_date"] = yesterday
        entry["cycle_start_date"] = today
        entry["cycle_note"] = str(note or "")[:60]
        entry["active"] = True
        return (key, old_acc, old_start)

    return update_state(_apply)


def set_crop_target(crop_name: str, target):
    """
    設定／清除某作物的自訂成熟目標積溫。target 為 None 表示清除、回到資料庫預設。
    回傳 (實際作物鍵, 生效後的目標, 是否找到作物)。
    """
    from science.gdd import lookup_crop_info, match_crop_key  # 延遲匯入
    raw = str(crop_name or "").strip()[:40]

    def _apply(state):
        crops = state.setdefault("crops", {})
        key = find_tracked_crop(crops, raw)
        if key is None:
            return (match_crop_key(raw), None, False)
        entry = crops.get(key)
        if not isinstance(entry, dict):
            entry = {"accumulated_gdd": float(entry or 0.0), "last_gdd_date": ""}
            crops[key] = entry
        if target is None:
            entry.pop("target_gdd", None)
        else:
            entry["target_gdd"] = round(float(target), 1)
        _, info = lookup_crop_info(key)
        return (key, crop_target_gdd(state, key, info), True)

    return update_state(_apply)


def save_state(state_dict):
    """
    將最新狀態保存至 state.json（持鎖 + 原子寫入，防止併發覆蓋與半截毀檔）。
    """
    try:
        with STATE_FILE_LOCK:
            atomic_write_json(STATE_FILE, state_dict)
        logger.info(f"✅ [State Manager] 狀態已更新: {state_dict}")
    except Exception as e:
        logger.error(f"❌ 寫入 state.json 失敗: {e}")


def update_state(mutator):
    """
    交易性更新 state.json：在持鎖狀態下一氣呵成完成「讀-改-寫」三步。
    單把鎖只保護單次讀或單次寫並不夠——「load_state → 修改 → save_state」
    之間鎖是放開的，並發的更新路徑（GDD 結算、AI 工具調閾值/換作物、
    手動指令）會互相覆蓋（lost update）。所有狀態更新一律改經本函式。
    mutator 接收 state dict 並就地修改，其回傳值即為本函式的回傳值。
    （STATE_FILE_LOCK 為 RLock，內部的 load/save 重入取鎖是安全的。）
    """
    with STATE_FILE_LOCK:
        state = load_state()
        result = mutator(state)
        save_state(state)
        return result
