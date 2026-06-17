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
# Gemini Agentic 會話管理 (Agent Session)
# ======================================================================
# 使用 google-genai SDK（新版；舊的 google-generativeai 已 EOL）。
# 建構帶 14 個工具的 Gemini 2.5 Flash 對話；對話 session 每日輪替防膨脹，
# per-chat asyncio.Lock 序列化並發呼叫；另提供無狀態一次性呼叫
# （定時推播與哨兵用）及其「停用工具」的防呆變體。
#
# 新舊 SDK 對應（遷移備忘）：
#   genai.configure(key) + GenerativeModel(...).start_chat(AFC=True)
#     → genai.Client(key).chats.create(model, config=GenerateContentConfig(tools=...))
#   model.generate_content(parts)
#     → client.models.generate_content(model, contents=parts, config=...)
#   傳入 Python 函式為 tools 時，SDK 預設啟用 automatic function calling（ReAct 迴路），
#   行為與舊版 enable_automatic_function_calling=True 一致。
#   429 限流：舊 google.api_core.exceptions.ResourceExhausted → 新 errors.ClientError(code=429)。
import asyncio
import time

from google import genai
from google.genai import errors, types

from agent.prompts import SYSTEM_INSTRUCTION
from agent.tools import AGENT_TOOLS
from config import GEMINI_API_KEY, now_taipei
from logging_setup import logger

MODEL_NAME = "gemini-2.5-flash"
AFC_MAX_CALLS = 10  # 單輪 ReAct 迴路最多自動執行幾次工具呼叫（防失控連環呼叫）

_client = genai.Client(api_key=GEMINI_API_KEY)

# 帶完整工具與系統指令的生成設定（取代舊版可重用的 GenerativeModel 物件）。
_AGENT_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    tools=AGENT_TOOLS,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=AFC_MAX_CALLS),
)

# 防呆重試專用：不帶工具，模型只能把分析寫成文字。
_NO_TOOLS_CONFIG = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)


def is_transient_api_error(e) -> bool:
    """
    是否為「稍後重試即可」的暫時性 API 錯誤：
    - 429 限流（ClientError(code=429)，免費額度尖峰常見）
    - 5xx 伺服器端錯誤（ServerError 涵蓋 500/502/503/504；
      例如 503 UNAVAILABLE「model is experiencing high demand」就屬此類）
    4xx 的其餘錯誤（如 400 參數錯誤、403 金鑰失效）重試也不會好，不在此列。
    """
    if isinstance(e, errors.ServerError):
        return True
    return isinstance(e, errors.ClientError) and getattr(e, "code", None) == 429


def _call_with_retry(fn, max_retries=3, delay=65):
    """
    呼叫 Gemini API 的強健包裝器：自動處理暫時性錯誤（429 限流與 5xx 壅塞）
    並指數退避重試，其餘錯誤一律向外拋出。fn 為無參數的可呼叫物件。
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except errors.APIError as e:
            if not is_transient_api_error(e):
                raise
            code = getattr(e, "code", "?")
            logger.warning(f"⚠️ [Gemini API] 收到暫時性錯誤 HTTP {code}。嘗試第 {attempt + 1} 次重試，等待 {delay} 秒...")
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def build_agent_model():
    """回傳帶 14 工具與系統指令的生成設定（新 SDK 以 config 取代舊的 GenerativeModel 物件）。"""
    return _AGENT_CONFIG


def start_new_gemini_chat():
    """
    建立新的 agentic 對話 session。傳入 Python 函式為 tools 時，SDK 自動執行
    「模型請求工具 → 本地執行 → 結果回填 → 模型繼續推理」的 ReAct 迴路，
    直到模型產出最終文字回覆為止（與舊版 enable_automatic_function_calling=True 一致）。
    """
    return _client.chats.create(model=MODEL_NAME, config=_AGENT_CONFIG)


def send_message_with_retry(chat, prompt, max_retries=3, delay=65):
    """對既有對話 session 送出訊息，帶 429 限流的指數退避重試。"""
    return _call_with_retry(lambda: chat.send_message(prompt), max_retries, delay)


def generate_oneshot_with_retry(prompt_parts, max_retries=3, delay=65):
    """
    無狀態的一次性 agentic 呼叫，供定時推播與安全哨兵使用：
    建立帶工具的臨時 chat，送出單一訊息後即丟棄——模型在這一輪內仍可自主
    呼叫工具進行多步調查，但不留任何跨輪 context。自動重試 429 並指數退避。
    """
    def _do():
        chat = _client.chats.create(model=MODEL_NAME, config=_AGENT_CONFIG)
        return chat.send_message(prompt_parts)
    return _call_with_retry(_do, max_retries, delay)


def generate_oneshot_single_tool(prompt_parts, tool_fn, max_retries=3, delay=65):
    """
    最小權限的聚焦一次性呼叫：只攜帶「單一工具」。供純副作用任務
    （如每日預測登記）使用——若給滿 14 個工具，模型在這種無人審視輸出的
    輪次裡可能多此一舉地再啟動爬蟲（2 分鐘、搶 Chromium 鎖），
    甚至呼叫改寫狀態的工具。限縮工具面即從根本上排除這些可能。
    """
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[tool_fn],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=3),
    )
    return _call_with_retry(
        lambda: _client.models.generate_content(
            model=MODEL_NAME, contents=prompt_parts, config=cfg),
        max_retries, delay)


def generate_oneshot_no_tools(prompt_parts, max_retries=3, delay=65):
    """
    無狀態、且「不帶工具」的一次性呼叫。用於防呆重試：當帶工具的呼叫
    漏掉分析本體時，以純文字模式強制模型直接產出文字報告——沒有工具可呼叫，
    它就只能把分析寫出來。
    """
    return _call_with_retry(
        lambda: _client.models.generate_content(
            model=MODEL_NAME, contents=prompt_parts, config=_NO_TOOLS_CONFIG),
        max_retries, delay)


chat_sessions = {}  # chat_id -> {"chat": Chat, "created_date": "YYYY-MM-DD"}
chat_locks = {}     # chat_id -> asyncio.Lock，序列化同一對話的 Gemini 呼叫


def get_chat_lock(chat_id) -> "asyncio.Lock":
    """
    取得（或建立）某對話專屬的 asyncio.Lock。
    Chat session 不是 thread-safe：使用者在前一則訊息還在處理時追問第二句
    （爬蟲工具一跑就是一兩分鐘，這情境非常常見），兩條 to_thread 執行緒
    並發呼叫同一個 session 的 send_message 會交錯污染對話歷史。
    同一對話的 Gemini 呼叫必須持本鎖排隊。僅應在事件迴圈內呼叫。
    """
    lock = chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        chat_locks[chat_id] = lock
    return lock


def _today_taipei_str() -> str:
    return now_taipei().strftime("%Y-%m-%d")


def is_session_fresh(chat_id) -> bool:
    """
    此對話今天是否尚無活躍 session——即下一則訊息將以「全新記憶」開始
    （容器重啟、每日輪替後的第一則訊息）。供 handlers 在 prompt 中提醒模型
    誠實面對記憶重置，而非對使用者指涉的舊對話裝懂。
    """
    entry = chat_sessions.get(chat_id)
    return not (entry and entry.get("created_date") == _today_taipei_str())


def get_or_create_chat(chat_id):
    """
    取得 Telegram 對話的 Gemini session；每天自動輪替重置一次，
    防止對話 context 跨日永續膨脹（重要的長期狀態都存在 state.json，
    不依賴對話記憶，輪替不會遺失作物設定與積溫）。
    """
    today = _today_taipei_str()
    entry = chat_sessions.get(chat_id)
    if entry and entry.get("created_date") == today:
        return entry["chat"]
    if entry:
        logger.info(f"♻️ [Session Manager] 對話 session 已逾一日，自動輪替重置 (chat_id={chat_id})。")
    chat = start_new_gemini_chat()
    chat_sessions[chat_id] = {"chat": chat, "created_date": today}
    return chat


def reset_chat(chat_id):
    """手動重置指定對話的 session（/reset 指令用）。"""
    chat = start_new_gemini_chat()
    chat_sessions[chat_id] = {"chat": chat, "created_date": _today_taipei_str()}
    return chat
