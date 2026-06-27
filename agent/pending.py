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
# 待確認事件機制 (Pending Event Confirmation)
# ======================================================================
# 收成與施肥是會寫進長期記錄、影響週期歸納的事件，故不讓 AI 從自由文字
# 直接寫入，而是先登記為「待確認」、由使用者一句話確認後才落檔，
# 以防 AI 誤判（如「我在想要不要收成」「鄰居收成了」）污染採收節律資料。
# 確認採「預設會記、給喊停視窗」：
#   - 明確肯定 → 立即落檔；明確否定（不要/取消…）→ 丟棄；
#   - 不明確或沉默 → 確認窗（TTL）逾時後由 watchdog 視為默認同意自動落檔
#     （與 AI 告知使用者的「將為你登記，若不用請喊停」承諾一致——
#      沉默絕不能變成資料默默遺失）。
# 確認的攔截邏輯在 tg/handlers.py 的訊息入口；逾時落檔在 watchdog_loop 的 sweep。
import contextvars
import threading
import time

from logging_setup import logger
from storage.events import save_fertilizer_event
from storage.harvest import record_harvest

PENDING_EVENTS = {}  # chat_id -> {"type": "harvest"/"fertilizer", "note": str, "created_epoch": float}
PENDING_EVENT_TTL = 600  # 確認窗 10 分鐘：期間可喊停，逾時視為默認同意自動落檔
# 保護 PENDING_EVENTS：確認(commit)、取消(clear)、逾時 sweep、AI 登記(set) 可能來自
# 不同執行緒（asyncio task / to_thread 工具 / watchdog 執行緒）。鎖讓「認領事件」
# （get+pop）成為原子操作，確保同一筆事件只會被一條路徑落檔一次，落檔本身在鎖外執行。
_pending_lock = threading.Lock()

# 當前正在處理訊息的 chat_id，供 AFC 工具回寫待確認槽使用。
# 使用 contextvars 而非普通全域變數：訊息以 asyncio.create_task 併發處理，
# 普通全域變數會被後到的訊息覆蓋（競態），導致事件可能登記到錯誤對話；
# contextvars 為每個 asyncio task 維持獨立的值，天然隔離併發訊息。
_current_chat_ctx = contextvars.ContextVar("current_chat_id", default=None)


def set_current_chat_context(chat_id):
    """handle_message 進入時設定，讓 AI 工具知道事件該登記給哪個 chat。"""
    _current_chat_ctx.set(chat_id)


def get_current_chat_context():
    """取得當前 asyncio task 綁定的 chat_id（無則 None）。"""
    return _current_chat_ctx.get()


def _set_pending_event(chat_id, ev_type, note):
    """
    登記一筆待確認事件。若該對話已有未決事件（前一個尚未被使用者確認/取消），
    不覆蓋——保留先發起的事件,避免「你以為在確認 A、實際確認到 B」的張冠李戴。
    回傳 True 表示成功登記,False 表示因已有未決事件而被擱置。
    """
    with _pending_lock:
        existing = PENDING_EVENTS.get(chat_id)  # 直查字典：已逾時待自動落檔者同樣不可覆蓋
        if existing:
            logger.warning(f"⚠️ [Pending Event] 對話 {chat_id} 已有未決的「{existing['type']}」確認，"
                           f"本次「{ev_type}」登記暫不覆蓋,待前者處理後再試。")
            return False
        PENDING_EVENTS[chat_id] = {
            "type": ev_type,
            "note": note,
            "created_epoch": time.time(),
        }
        return True


def get_pending_event(chat_id):
    """取得仍在確認窗內的待確認事件。已逾時者回傳 None 但不丟棄——
    逾時事件視為默認同意，由 watchdog 的 sweep 自動落檔。"""
    ev = PENDING_EVENTS.get(chat_id)
    if not ev:
        return None
    if time.time() - ev["created_epoch"] > PENDING_EVENT_TTL:
        return None
    return ev


def clear_pending_event(chat_id):
    with _pending_lock:
        PENDING_EVENTS.pop(chat_id, None)


def _apply_pending_event(ev) -> str:
    """
    依事件類型實際執行落檔，回傳結果訊息。
    僅收成/施肥走確認制（它們寫進不可逆的長期記錄）；作物追蹤加入/停止
    為低風險可逆操作，已改由工具立即生效、不經本機制。
    （commit 與逾時 sweep 共用此單一分派，確保兩條路徑行為一致。）
    """
    etype = ev["type"]
    note = ev.get("note", "")
    if etype == "harvest":
        return record_harvest(note)
    if etype == "fertilizer":
        save_fertilizer_event(note or "使用者登記施肥")
        return f"🧪 已記錄施肥事件：{note}"
    return ""


def commit_pending_event(chat_id) -> str:
    """將待確認事件實際落檔。回傳結果訊息（無事件可落檔時回空字串）。
    直接以 pop 取走字典中的事件（即使剛逾時也照常落檔），
    避免「明確確認與逾時擦肩」的競態造成漏記或重複記。"""
    with _pending_lock:  # 原子認領：與 sweep／cancel 互斥，確保只有一條路徑拿到此事件
        ev = PENDING_EVENTS.pop(chat_id, None)
    if not ev:
        return ""
    return _apply_pending_event(ev)  # 落檔在鎖外做（不在持鎖時做 DB I/O）


def sweep_expired_pending_events() -> list:
    """
    將逾時未獲回覆的待確認事件視為「默認同意」自動落檔，
    實作「預設會記、給喊停視窗」的承諾——使用者看到 AI 說「將為你登記」
    之後沉默離開，事件仍會被記錄，而非默默蒸發。
    由 watchdog 迴圈每分鐘呼叫。回傳 [(chat_id, 通知訊息), ...] 供發送 Telegram。
    """
    results = []
    now_epoch = time.time()
    for cid in list(PENDING_EVENTS.keys()):
        # 原子認領：在鎖內判斷逾時並 pop，確保不會與使用者同時確認/取消擦肩而重複落檔。
        with _pending_lock:
            ev = PENDING_EVENTS.get(cid)
            if not ev or now_epoch - ev["created_epoch"] <= PENDING_EVENT_TTL:
                ev = None
            else:
                PENDING_EVENTS.pop(cid, None)
        if not ev:
            continue
        try:
            msg = _apply_pending_event(ev)  # 落檔在鎖外做
            results.append((cid, f"⏱️ 確認窗已逾時，依預設為您自動完成：\n{msg}"))
            logger.info(f"⏱️ [Pending Event] 逾時默認同意，已自動處理 {ev['type']} (chat_id={cid})。")
        except Exception as e:
            logger.error(f"❌ [Pending Event] 逾時自動落檔失敗: {e}")
    return results


# 確認/取消的語氣偵測
# 否定優先（任一否定詞即視為取消）。肯定判定分兩層：
#   1. 「整句完全等於」的短確認詞（好/對/可以…）——只在獨立成句時算數。
#      這些高頻字若用子字串比對，「對了，明天會下雨嗎」「可以幫我看葉子嗎」
#      都會被誤判成確認：錯誤落檔之餘，還會提前 return 吞掉使用者真正的問題。
#   2. 子字串比對僅保留「明確的登記措辭」（幫我記/請登記…），且句中含問號時
#      一律不視為肯定（問句不是確認）。
# 從寬判成 unclear 沒有資料遺失風險：確認窗逾時後 watchdog 仍會默認落檔。
_DENY_WORDS = ["不要", "不用", "不是", "取消", "別記", "別登", "不對", "錯了", "先不", "算了", "不需要", "沒有要"]
_AFFIRM_EXACT = ("對", "是", "好", "嗯", "嗯嗯", "ok", "okay", "yes", "對啊", "是啊",
                 "好的", "好啊", "好喔", "沒錯", "正確", "確認", "可以")
_AFFIRM_PHRASES = ["是的", "沒錯", "麻煩你", "麻煩幫", "幫我記", "幫我登", "請記", "請登",
                   "記一下", "記下來", "確認登記", "確認記錄", "登記吧", "記錄吧"]


def classify_confirmation(text: str):
    """判斷使用者回覆是肯定、否定，還是不明確。回傳 'yes'/'no'/'unclear'。"""
    t = (text or "").strip().lower()
    if not t:
        return "unclear"
    if any(w in t for w in _DENY_WORDS):
        return "no"
    # 第一層：去除結尾標點與語助詞後「整句完全等於」短確認詞
    t_norm = t.rstrip("!！。．.~～嘛呀啦喔哦 ")
    if t_norm in _AFFIRM_EXACT:
        return "yes"
    is_question = ("?" in t or "？" in t)
    if not is_question:
        # 第二層：明確登記措辭的子字串比對（問句不算）
        if any(w in t for w in _AFFIRM_PHRASES):
            return "yes"
        # 第三層：明確的「確認/確定＋記錄/登記」組合，或「要記/要登」意圖——
        # 即使整句較長、帶作物名（如「確認要記錄空心菜」「確定要登記」）也是清楚肯定。
        # 這些詞單獨出現（如「確認天氣」「我要看記錄」）不含對方→不誤判。
        has_confirm = any(w in t for w in ("確認", "確定", "確實", "就這樣", "對，", "對,"))
        if has_confirm and ("記" in t or "登" in t):
            return "yes"
        if "要記" in t or "要登" in t:
            return "yes"
    return "unclear"
