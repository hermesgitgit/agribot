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
# Command Guard 防護欄：AI 回覆的文字面清洗（舊版控制標記只清不套用、連結移除）
# ======================================================================
# Gemini 的輸入包含爬取的網頁文字（阿龜建議、CWA 預報），存在間接注入誘導
# 模型輸出控制指令的理論路徑；模型本身也可能幻覺出離譜數值。
# 因此所有閉環指令統一經過本模組驗證後才允許改寫 state.json；
# AI 回覆中的連結一律攔截移除（Link Guard，縱深防禦）。
import re

from logging_setup import logger


MAX_THRESHOLD_STEP = 15.0  # AI 單次調整警戒門檻的最大允許幅度 (百分點)


_LEGACY_MARKER = re.compile(r'\[SET_(?:THRESHOLD|CROP):[^\]]*\]')


def strip_legacy_markers(ai_message: str, source_tag: str = "AI") -> str:
    """
    清掉 AI 回覆裡殘留的舊版文字標記 [SET_THRESHOLD: …]／[SET_CROP: …]，**一律不套用**。
    歷史：早期是教模型在回覆裡夾這種標記、再由這裡解析套用到 state.json；改成 Function
    Calling（tool_set_thresholds／tool_set_crop）之後系統指令已不再教它，但解析器留著就是
    一條只靠「模型不要複述」撐著的旁路——爬到的網頁或知識庫段落夾帶這串字，模型依安全鐵則
    「引述可疑內容提醒使用者」，門檻或作物就真的被改了。2026-10-31 拆掉：出現就記 warning
    （這是注入的訊號）、從訊息裡移除，狀態一概不動。要改門檻／作物只有工具呼叫一條路，
    而那條路有 tools.py 的數值範圍與當日累計上限把關。
    """
    def _repl(m):
        logger.warning(f"🛡️ [Command Guard / {source_tag}] 回覆中出現舊版控制標記，已移除且未套用（疑似注入）: {m.group(0)[:80]}")
        return ""
    return _LEGACY_MARKER.sub(_repl, ai_message).strip()


URL_PATTERN = re.compile(r'(?:https?://|www\.)[^\s\)\]」』>]+', re.IGNORECASE)


def strip_links(ai_message: str, source_tag: str = "AI") -> str:
    """移除 AI 回覆中的所有網址（縱深防禦），回傳清洗後的訊息文字。"""
    def _repl(m):
        logger.warning(f"🛡️ [Link Guard / {source_tag}] 已攔截 AI 回覆中的連結: {m.group(0)[:80]}")
        return "〔連結已由系統移除〕"
    return URL_PATTERN.sub(_repl, ai_message)
