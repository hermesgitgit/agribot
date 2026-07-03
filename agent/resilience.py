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
# 產報告的韌性包裝 (Report Generation Resilience)
# ======================================================================
# 把「先帶工具產出；若非壅塞性崩潰就退無工具重產」的控制流，抽成一個
# 不依賴任何模型 SDK 的高階函式——這樣它可被獨立測試（注入假的產出函式
# 模擬崩潰），也不必為了測它而載入 google-genai。
from config import redact
from logging_setup import logger


def generate_report_resilient(prompt_parts, gen_with_tools, gen_no_tools, is_transient):
    """
    先用「帶工具」的方式產報告；若帶工具的呼叫崩潰：
      - 壅塞性錯誤（is_transient(exc) 為真）→ 往外拋，交由呼叫端的備援模型處理；
      - 其他崩潰（例：模型發出不存在的工具名，導致 SDK 的自動工具呼叫丟 KeyError）
        → 退到「無工具純文字」重產：沒有工具可呼叫就踩不到那顆雷，而報告所需
        資料本就都寫在 prompt 裡。
    gen_with_tools / gen_no_tools 皆為 callable(prompt_parts) -> response；
    is_transient 為 callable(exc) -> bool。回傳所選路徑的 response。
    """
    try:
        return gen_with_tools(prompt_parts)
    except Exception as gen_err:
        if is_transient(gen_err):
            raise
        logger.warning(f"⚠️ 帶工具的分析呼叫異常（{redact(gen_err)}），改用無工具純文字重產…")
        return gen_no_tools(prompt_parts)
