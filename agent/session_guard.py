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
# 請求層級的「已取消」標記（2026-11-02 審查修正 B，A3）
# ======================================================================
# handle_message 被逾時取消後，asyncio.to_thread 裡的模型執行緒還會繼續跑完 AFC 迴路，
# 途中可能才真的呼叫 tool_set_crop／tool_record_fertilizer_event 等寫入型工具——使用者
# 早已收到「處理逾時」，卻在幾十秒後被默默改了狀態。這裡提供每個請求一個 threading.Event：
# 由 agent/session.py 的 run_model_turn 在呼叫端 task 的 context 設好（to_thread 會複製
# context 進工作執行緒），逾時時 set()；寫入型工具執行前以 cancelled_write_guard() 檢查。
# 獨立成小模組的原因：agent/tools.py 要用它，而 agent/session.py 又 import tools，
# 放在 session.py 會循環引用。
import contextvars

_cancel_ctx = contextvars.ContextVar("request_cancel_flag", default=None)
CANCELLED_WRITE_NOTICE = "⏱️ 本輪已逾時，未寫入（請使用者重新下一次指令）。"


def is_request_cancelled() -> bool:
    """目前這個請求（asyncio task 或其 to_thread 工作執行緒）是否已被逾時取消。"""
    flag = _cancel_ctx.get()
    return bool(flag is not None and flag.is_set())


def cancelled_write_guard():
    """寫入型工具的統一守門：已取消回提示字串（呼叫端直接 return），否則回 None。"""
    return CANCELLED_WRITE_NOTICE if is_request_cancelled() else None


def mark_request_cancelled():
    """把目前請求標記為已取消（run_model_turn 逾時分支與測試用）。"""
    flag = _cancel_ctx.get()
    if flag is not None:
        flag.set()
