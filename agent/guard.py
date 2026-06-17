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
# Command Guard 防護欄：AI 閉環指令的集中解析、驗證與套用
# ======================================================================
# Gemini 的輸入包含爬取的網頁文字（阿龜建議、CWA 預報），存在間接注入誘導
# 模型輸出控制指令的理論路徑；模型本身也可能幻覺出離譜數值。
# 因此所有閉環指令統一經過本模組驗證後才允許改寫 state.json；
# AI 回覆中的連結一律攔截移除（Link Guard，縱深防禦）。
import re

from logging_setup import logger
from science.gdd import CROP_GDD_DATABASE
from storage.state import update_state


MAX_THRESHOLD_STEP = 15.0  # AI 單次調整警戒門檻的最大允許幅度 (百分點)


def apply_threshold_command(ai_message: str, source_tag: str = "AI") -> str:
    """
    從 Gemini 回覆中攔截 [SET_THRESHOLD: ...] 指令，通過安全驗證才套用至 state.json，
    並回傳已清除隱藏標記的訊息文字。驗證規則：
      1. 必須滿足 0 < dry < wet < 100（否則哨兵警報邏輯會永久觸發或永久失效）。
      2. 單次調幅不得超過 ±MAX_THRESHOLD_STEP 個百分點（防範注入/幻覺一步到位地癱瘓監控；
         確需大幅調整時請使用 /threshold 手動指令）。
    不合規的指令一律丟棄並記錄 log，僅清除標記、不改寫任何狀態。
    """
    threshold_match = re.search(r'\[SET_THRESHOLD:\s*dry=([0-9\.]+),\s*wet=([0-9\.]+),\s*state=(\w+)[^\]]*\]', ai_message)
    if not threshold_match:
        return ai_message
    cleaned = re.sub(r'\[SET_THRESHOLD:[^\]]*\]', '', ai_message).strip()
    try:
        new_dry = float(threshold_match.group(1))
        new_wet = float(threshold_match.group(2))
        new_state = threshold_match.group(3)

        if not (0.0 < new_dry < new_wet < 100.0):
            logger.warning(f"🛡️ [Command Guard] 拒絕 SET_THRESHOLD：數值不滿足 0 < dry({new_dry}) < wet({new_wet}) < 100。")
            return cleaned

        def _apply(state):
            cur_dry = float(state.get("dry_threshold", 30.0))
            cur_wet = float(state.get("wet_threshold", 80.0))
            if abs(new_dry - cur_dry) > MAX_THRESHOLD_STEP or abs(new_wet - cur_wet) > MAX_THRESHOLD_STEP:
                logger.warning(f"🛡️ [Command Guard] 拒絕 SET_THRESHOLD：單次調幅超過 ±{MAX_THRESHOLD_STEP} 百分點 (dry {cur_dry}->{new_dry}, wet {cur_wet}->{new_wet})。")
                return False
            state["lifecycle"] = f"{new_state} (AI 自主評估生長階段)"
            state["dry_threshold"] = new_dry
            state["wet_threshold"] = new_wet
            return True

        if update_state(_apply):
            logger.info(f"🔄 [Agentic Closed-Loop / {source_tag}] 閾值已更新: dry={new_dry}%, wet={new_wet}%, state={new_state}")
    except Exception as set_err:
        logger.warning(f"⚠️ [{source_tag}] 解析 SET_THRESHOLD 指令失敗: {set_err}")
    return cleaned


def apply_crop_command(ai_message: str, source_tag: str = "AI") -> str:
    """
    從 Gemini 回覆中攔截 [SET_CROP: name=...] 指令，模糊比對資料庫後套用，
    並回傳已清除隱藏標記的訊息文字。名稱截斷至 40 字元以內，防範超長字串注入。
    """
    crop_match = re.search(r'\[SET_CROP:\s*name=([^\]]+)\]', ai_message)
    if not crop_match:
        return ai_message
    cleaned = re.sub(r'\[SET_CROP:[^\]]*\]', '', ai_message).strip()
    try:
        new_crop = crop_match.group(1).strip()[:40]
        if not new_crop:
            return cleaned
        matched_crop = None
        for k in CROP_GDD_DATABASE.keys():
            if new_crop.split()[0] in k or k.split()[0] in new_crop:
                matched_crop = k
                break
        if not matched_crop:
            matched_crop = new_crop

        def _apply(state):
            state["crop_name"] = matched_crop
            if matched_crop not in state.setdefault("crops", {}):
                state["crops"][matched_crop] = {
                    "accumulated_gdd": 0.0,
                    "last_gdd_date": ""
                }
            return state["crops"][matched_crop]["accumulated_gdd"]

        acc_gdd = update_state(_apply)
        logger.info(f"🔄 [Agentic Closed-Loop / {source_tag}] 作物已切換為: {matched_crop}，目前該作物 GDD: {acc_gdd}")
    except Exception as set_err:
        logger.warning(f"⚠️ [{source_tag}] 解析 SET_CROP 指令失敗: {set_err}")
    return cleaned


URL_PATTERN = re.compile(r'(?:https?://|www\.)[^\s\)\]」』>]+', re.IGNORECASE)


def strip_links(ai_message: str, source_tag: str = "AI") -> str:
    """移除 AI 回覆中的所有網址（縱深防禦），回傳清洗後的訊息文字。"""
    def _repl(m):
        logger.warning(f"🛡️ [Link Guard / {source_tag}] 已攔截 AI 回覆中的連結: {m.group(0)[:80]}")
        return "〔連結已由系統移除〕"
    return URL_PATTERN.sub(_repl, ai_message)
