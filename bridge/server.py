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
# Hermes Agent 橋接端點（選用）
# ======================================================================
# 讓同一台 NAS 上的 Hermes 容器可以把農務問題「借用」既有的 AI 對話/本地指令
# 邏輯處理，答案原文回傳——不觸碰真正的 Telegram 頻道（經 tg/api.py 的擷取
# 模式攔截）。只監聽內部 Docker 網路、且要求共用密鑰；未設定密鑰則整條停用。
#
# 兩條路徑：
#   POST /ask   {"text": "..."}      走完整 AI 對話（handle_message），會產生
#                                     Gemini API 呼叫與花費，僅用於真正需要
#                                     推理的問題。
#   POST /local {"command": "/status"} 走本地指令（handle_local_command），
#                                     零 AI 額度、秒回，供簡單狀態查詢使用。
#
# 以獨立執行緒跑 ThreadingHTTPServer（零額外依賴），透過
# asyncio.run_coroutine_threadsafe 把協程排進主事件迴圈執行並阻塞等結果——
# 沿用既有的 chat lock／pending 事件邏輯，與真正的 Telegram 訊息安全序列化。
import asyncio
import concurrent.futures
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import BRIDGE_PORT, BRIDGE_SHARED_SECRET, TELEGRAM_CHAT_ID, redact
from logging_setup import logger
from tg.api import start_capture, stop_capture
from tg.handlers import handle_local_command, handle_message

_main_loop = None  # 由 bridge_server_loop() 於啟動時取得，供跨執行緒排程協程

# 唯讀本地指令白名單（伺服器端強制）。橋接的 /local 只准「讀」：任何會寫入
# 農園狀態/長期記錄的指令（/threshold、/crop*、/harvest、/reset…）一律拒絕——
# 寫入必須走 /ask 交給本系統的 AI 判斷（經 Command Guard 與待確認機制）。
# Hermes 端 farm.py 也有同一份白名單，但那只是客戶端禮貌；Hermes 容器上跑的是
# 會執行任意指令的 agent，防線必須設在本端才算數。
READONLY_LOCAL_COMMANDS = frozenset({
    "/status", "/health", "/gdd", "/crops", "/et0", "/eto",
    "/disease", "/risk", "/harvest_stats", "/harveststats", "/cadence",
})

# 請求 body 上限：/ask 的 text 最長 4000 字元（UTF-8 中文最多約 12KB），加上 JSON
# 包裝取 16KB 綽綽有餘。超過直接回 413，不把整包讀進記憶體。
MAX_BODY_BYTES = 16 * 1024


# 橋接 /ask 的併發閘門（在主 asyncio 迴圈上建立，見 _ensure_gate）。
_ASK_GATE = asyncio.Lock()


def _run_coro_blocking(coro, timeout):
    """把協程排到主 asyncio 迴圈執行，在目前（HTTP handler）執行緒阻塞等結果。"""
    fut = asyncio.run_coroutine_threadsafe(coro, _main_loop)
    try:
        return fut.result(timeout=timeout)
    except (TimeoutError, concurrent.futures.TimeoutError):
        # 逾時就取消，別讓協程在主迴圈裡繼續跑（否則 Hermes 已收到 504，agribot 卻
        # 還在燒 Gemini、改 pending、稍後才落檔）。對「已開始執行」的協程 cancel 是
        # best-effort：會在下一個 await 點丟 CancelledError；_ask/_local 用 try/finally
        # 包著 start/stop_capture，取消時 finally 仍會跑到 stop_capture，狀態不殘留。
        fut.cancel()
        raise


async def _ask(text: str) -> str:
    # 橋接請求彼此序列化：逾時被 cancel 時，handle_message 裡那個
    # asyncio.to_thread 的 Gemini 呼叫「不會」跟著停（它在別的執行緒重試、可能
    # 還在 sleep 130 秒），但 CancelledError 會讓對話鎖的 async with 立刻退出。
    # 於是下一個請求或擁有者的訊息會拿到鎖、對同一個 ChatSession 併發送出。
    # 這道閘門確保同一時間只有一個橋接請求在飛；擁有者側的鎖仍由 handlers 負責。
    async with _ASK_GATE:
        token = start_capture()
        try:
            message = {"chat": {"id": int(TELEGRAM_CHAT_ID)}, "text": text}
            await handle_message(message)
        finally:
            reply = stop_capture(token)
        return reply


async def _local(command_text: str):
    token = start_capture()
    try:
        handled = await handle_local_command(int(TELEGRAM_CHAT_ID), command_text)
    finally:
        reply = stop_capture(token)
    return reply if handled else None


class _Handler(BaseHTTPRequestHandler):
    # 連線 socket 逾時：慢速/半開連線最多佔用執行緒 30 秒就斷，防 slow-loris 拖住
    # ThreadingHTTPServer 的執行緒。只作用於 socket 讀寫；/ask 等 Gemini 的 180 秒是在
    # asyncio 主迴圈裡等、不佔 socket 操作，所以不受這個 timeout 影響。
    timeout = 30

    def log_message(self, fmt, *args):
        pass  # 靜音 http.server 預設的存取日誌，改由 logging_setup 統一記錄

    def _reply_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # 密鑰採常數時間比對（compare_digest）：雖只暴露於內部 Docker 網路，
        # 仍不給旁路計時猜密鑰留任何空間。
        supplied = self.headers.get("X-Bridge-Secret") or ""
        if not BRIDGE_SHARED_SECRET or not hmac.compare_digest(supplied, BRIDGE_SHARED_SECRET):
            self._reply_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._reply_json(400, {"error": "bad request body"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            # body 沒讀就回覆：socket 上殘留未讀資料，這條連線不能再重用
            self.close_connection = True
            self._reply_json(413, {"error": f"body too large (max {MAX_BODY_BYTES} bytes)"})
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
        except Exception:
            self._reply_json(400, {"error": "bad request body"})
            return

        try:
            if self.path == "/ask":
                text = str(data.get("text", "")).strip()[:4000]
                if not text:
                    self._reply_json(400, {"error": "missing text"})
                    return
                # /ask 只收自然語言：以 "/" 開頭的文字會被 handle_message 當本地指令
                # 直接執行（含 /threshold、/reset 等寫入操作），繞過 AI 判斷與白名單。
                # 唯讀指令請走 /local；寫入操作請用自然語言讓 AI 走既有防護流程。
                if text.startswith("/"):
                    self._reply_json(400, {"error": "slash commands not allowed on /ask; use /local (read-only) or natural language"})
                    return
                reply = _run_coro_blocking(_ask(text), timeout=180)
                self._reply_json(200, {"reply": reply})
            elif self.path == "/local":
                cmd_text = str(data.get("command", "")).strip()[:200]
                cmd_head = cmd_text.split()[0].lower() if cmd_text else ""
                if cmd_head not in READONLY_LOCAL_COMMANDS:
                    self._reply_json(403, {"error": f"command not in read-only whitelist: {sorted(READONLY_LOCAL_COMMANDS)}"})
                    return
                reply = _run_coro_blocking(_local(cmd_text), timeout=30)
                if reply is None:
                    self._reply_json(404, {"error": "unknown local command"})
                    return
                self._reply_json(200, {"reply": reply})
            else:
                self._reply_json(404, {"error": "not found"})
        except (TimeoutError, concurrent.futures.TimeoutError):
            # 註：Python 3.10（本映像檔）的 concurrent.futures.TimeoutError 與內建
            # TimeoutError 是不同類別，必須兩者都接，否則逾時會誤回 500。
            self._reply_json(504, {"error": "timeout"})
        except Exception as e:
            logger.error(f"❌ [Bridge] 處理請求失敗: {redact(e)}")
            self._reply_json(500, {"error": "internal error"})


async def bridge_server_loop():
    """啟動橋接 HTTP 伺服器（背景執行緒），與其他 asyncio 迴圈併行掛在 main() 的 gather 中。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    if not BRIDGE_SHARED_SECRET:
        logger.warning("⚠️ [Bridge] BRIDGE_SHARED_SECRET 未設定，橋接端點停用（Hermes 端將無法呼叫）。")
        return  # 提早結束這條 task；main() 其餘迴圈不受影響

    # 橋接是「選用」功能：起不來（port 被占、位址無效等）只停用它，不能拖垮整個 bot。
    # bridge_server_loop 掛在 main() 的 asyncio.gather 裡，這裡若拋例外會穿透 gather、
    # 可能讓 Telegram/sentinel/push 一起收掉——所以吞下例外、log 後 return。
    try:
        server = ThreadingHTTPServer(("0.0.0.0", BRIDGE_PORT), _Handler)
    except Exception as e:
        logger.error(f"❌ [Bridge] 無法在 port {BRIDGE_PORT} 啟動，橋接停用（Telegram/警報/推播不受影響）: {redact(e)}")
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="agribot-bridge")
    thread.start()
    logger.info(f"🌉 [Bridge] 橋接端點已啟動於 0.0.0.0:{BRIDGE_PORT}（僅供內部 Docker 網路呼叫，需共用密鑰）。")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        server.shutdown()
        raise
