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
from storage.state import start_crop_cycle

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


def _set_pending_event(chat_id, ev_type, note, crop_hint=""):
    """
    登記一筆待確認事件。若該對話已有未決事件（前一個尚未被使用者確認/取消），
    不覆蓋——保留先發起的事件,避免「你以為在確認 A、實際確認到 B」的張冠李戴。
    回傳 True 表示成功登記,False 表示因已有未決事件而被擱置。
    """
    # 橋接擷取模式（Hermes 經 /ask 呼叫）下不建立待確認事件：這個模式的回覆是
    # 回給 HTTP 呼叫端的，「若不用請喊停」那句話擁有者根本看不到，10 分鐘後
    # watchdog 就把它當默認同意寫進不可逆的長期記錄。等於「待確認」這道防線在
    # 擷取模式下完全不存在——而 Hermes 讀到的網頁文字是不可信輸入。
    from tg.api import is_capturing
    if is_capturing():
        logger.warning(f"⚠️ [Pending Event] 擷取模式（橋接）不建立「{ev_type}」待確認事件；"
                       f"登記類動作請由擁有者在 Telegram 直接操作。")
        return False
    with _pending_lock:
        existing = PENDING_EVENTS.get(chat_id)  # 直查字典：已逾時待自動落檔者同樣不可覆蓋
        if existing:
            logger.warning(f"⚠️ [Pending Event] 對話 {chat_id} 已有未決的「{existing['type']}」確認，"
                           f"本次「{ev_type}」登記暫不覆蓋,待前者處理後再試。")
            return False
        PENDING_EVENTS[chat_id] = {
            "type": ev_type,
            "note": note,
            "crop_hint": crop_hint,   # 落檔時決定記到哪個作物，不能只靠備註字串
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


class PendingWriteError(Exception):
    """待確認事件落檔失敗（2026-11-02 審查修正 B，A4）。"""


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
        return record_harvest(note, crop_hint=ev.get("crop_hint", ""))
    if etype == "fertilizer":
        # 2026-11-02 審查修正 B（A4）：落檔失敗不得回「已記錄」，拋例外讓呼叫端把事件放回待確認
        if not save_fertilizer_event(note or "使用者登記施肥"):
            raise PendingWriteError("events.json 寫入失敗")
        return f"🧪 已記錄施肥事件：{note}"
    if etype == "sow":
        key, old_acc, old_start = start_crop_cycle(ev.get("crop_hint", ""), note)
        prev = f"（上一輪自 {old_start or '不明'} 累計 {old_acc} ℃-day 已存檔）" if old_acc else ""
        return (f"🌱 已為「{key}」開始新一輪：今天起算、積溫歸零、納入每日追蹤。{prev}\n"
                f"首次採收時登記割收，累計到的積溫就會成為這塊地的成熟積溫樣本。")
    return ""


_EVENT_LABELS = {"harvest": "收成", "fertilizer": "施肥", "sow": "播種起算"}
PENDING_WRITE_MAX_RETRIES = 3  # 逾時 sweep 落檔連續失敗幾次後放棄（避免每分鐘無限重試洗 log）


def _restore_pending_event(chat_id, ev) -> bool:
    """落檔失敗：把事件放回待確認表（同一把鎖）。槽位已被新事件占走時不覆蓋、回 False。"""
    with _pending_lock:
        if PENDING_EVENTS.get(chat_id):
            return False
        PENDING_EVENTS[chat_id] = ev
        return True


def _write_failed_message(ev) -> str:
    label = _EVENT_LABELS.get(ev.get("type"), "登記")
    return f"⚠️ {label}記錄寫入失敗，事件仍保留在待確認，稍後再回『確認』重試。"


def commit_pending_event(chat_id) -> str:
    """將待確認事件實際落檔。回傳結果訊息（無事件可落檔時回空字串）。
    直接以 pop 取走字典中的事件（即使剛逾時也照常落檔），
    避免「明確確認與逾時擦肩」的競態造成漏記或重複記。
    2026-11-02 審查修正 B（A4）：落檔失敗（施肥 events.json／割收 DB／播種 state.json）
    時把事件放回 PENDING_EVENTS 並回失敗訊息，絕不回成功文字。"""
    with _pending_lock:  # 原子認領：與 sweep／cancel 互斥，確保只有一條路徑拿到此事件
        ev = PENDING_EVENTS.pop(chat_id, None)
    if not ev:
        return ""
    try:
        return _apply_pending_event(ev)  # 落檔在鎖外做（不在持鎖時做 DB I/O）
    except Exception as e:
        logger.error(f"❌ [Pending Event] {ev.get('type')} 落檔失敗，事件放回待確認: {e}")
        _restore_pending_event(chat_id, ev)
        return _write_failed_message(ev)


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
            # 2026-11-02 審查修正 B（A4）：落檔失敗不能讓事件蒸發——放回待確認表，下一分鐘再試；
            # 連續失敗達上限才放棄並通知使用者改手動登記
            ev["write_failures"] = int(ev.get("write_failures", 0)) + 1
            logger.error(f"❌ [Pending Event] 逾時自動落檔失敗（第 {ev['write_failures']} 次）: {e}")
            if ev["write_failures"] >= PENDING_WRITE_MAX_RETRIES:
                results.append((cid, _write_failed_message(ev).replace(
                    "事件仍保留在待確認，稍後再回『確認』重試",
                    f"已連續 {ev['write_failures']} 次無法寫入，請稍後手動再登記一次")))
            else:
                _restore_pending_event(cid, ev)
    return results


# 確認/取消的語氣偵測
# 2026-11-02 審查修正 B：只接受「指向該事件的明確回覆」。舊版用否定詞子字串掃描，
# 「今天不用澆水了」會被當成取消（待確認的收成被永久丟棄）、「麻煩你幫我查一下天氣」
# 因含「麻煩你」被當成確認而立刻落檔，而且 handler 攔截後直接 return，追問本身被吞掉。
# 新規則：
#   (a) 整句去掉標點／語助詞後「完全等於」短確認詞（_AFFIRM_EXACT）或短否定詞（_DENY_EXACT）；
#   (b) 句中同時含「事件關鍵詞」（收成／割收／施肥／播種／登記／記錄／記下…）與確認或否定詞；
#   (c) 明確的登記措辭（幫我記／請登記／記下來…）仍視為肯定——它們本身就指向登記這件事。
#   其餘一律 unclear：訊息照常進 AI 對話，待確認事件保留，逾時由 watchdog 默認落檔（行為不變）。
# 否定優先於肯定（「好，不用記了」是取消）；含問號的一律 unclear（問句不是確認也不是拒絕）。
_DENY_EXACT = ("不用", "不要", "取消", "不記", "免了", "算了", "no", "nope", "不是", "不對", "錯了",
               "先不要", "先不用", "不需要", "沒有要", "別記", "別登", "不必", "不用記", "不要記",
               "不用了", "不要了", "不必了", "取消吧", "不用啦", "不要啦")
_DENY_WORDS = ["不要", "不用", "不是", "取消", "別記", "別登", "不對", "錯了", "先不", "算了", "不需要",
               "沒有要", "免了", "不必"]
_AFFIRM_EXACT = ("對", "是", "好", "嗯", "嗯嗯", "ok", "okay", "yes", "對啊", "是啊", "是的",
                 "好的", "好啊", "好喔", "好吧", "沒錯", "正確", "確認", "可以", "確定", "要")
# 明確的取消措辭：本身就指向「不要登記這件事」，出現在句中即視為取消（「好，不用記了」）
_DENY_PHRASES = ["不用記", "不要記", "不記", "別記", "別登", "不用登", "不要登", "取消登記", "取消記錄"]
# 2026-11-02 審查修正 D（A1）：拿掉單獨的「要」——「我明天要施肥」的「要」是意向，不是確認；
# 「要記」「要登」仍留在 _AFFIRM_PHRASES，整句只有「要」一字者仍由 _AFFIRM_EXACT 判 yes。
_AFFIRM_WORDS = ["對", "好", "可以", "確認", "確定", "確實", "是的", "沒錯", "正確", "ok", "yes", "麻煩"]
# 明確的登記措辭：本身就指向「登記這件事」，單獨成立即可視為肯定（不含「麻煩你」這類泛用客套語）
_AFFIRM_PHRASES = ["幫我記", "幫我登", "請記", "請登", "記一下", "記下來", "確認登記", "確認記錄",
                   "登記吧", "記錄吧", "要記", "要登"]
# 事件關鍵詞：使用者的回覆提到這些詞，才算「指向待確認的收成／施肥／播種事件」
# 2026-11-02 審查修正 D（A1）：關鍵詞改依事件類型分組。舊版不分類型，待確認「收成」時
# 「我明天要施肥」因含「施肥」＋「要」被判 yes——收成被誤落檔、真正的新訊息還被吞掉。
# 現在只有「本事件類型」的詞或通用登記詞才算指向待確認事件；提到別種事件而沒提本事件 → unclear。
_EVENT_WORDS_BY_TYPE = {
    "harvest": ["收成", "割收", "採收"],
    "fertilizer": ["施肥", "追肥"],
    "sow": ["播種", "定植", "起算"],
}
_GENERIC_EVENT_WORDS = ["登記", "記錄", "紀錄", "記下", "這筆", "剛才那個"]
# 不知道事件類型時（event_type=None）退回舊行為：所有類型的詞都算
_EVENT_WORDS = [w for ws in _EVENT_WORDS_BY_TYPE.values() for w in ws] + _GENERIC_EVENT_WORDS
# 2026-11-02 審查修正 D（A1）：未來／意向標記——句中出現即表示這是一則新訊息（預告、打算），
# 不是對「已經發生的事件」的確認或拒絕，一律 unclear 交給 AI 對話處理。
_FUTURE_INTENT_MARKERS = ["明天", "後天", "下週", "下周", "之後", "等一下", "等下", "待會", "晚點",
                          "打算", "預計", "準備", "要去", "想要"]
# 中文正反問句（要不要／是不是…）內含否定詞，先剝掉再掃描，免得把問句當拒絕
_AB_QUESTION_PATTERNS = ("需不需要", "可不可以", "要不要", "用不用", "是不是", "對不對", "好不好", "能不能")


def classify_confirmation(text: str, event_type=None):
    """判斷使用者回覆是肯定、否定，還是不明確。回傳 'yes'/'no'/'unclear'。
    2026-11-02 審查修正 D（A1）：event_type 為目前待確認事件的類型（harvest／fertilizer／sow），
    事件關鍵詞依類型判定；None 時所有類型的詞都算（舊行為）。"""
    t = (text or "").strip().lower()
    if not t:
        return "unclear"
    if "?" in t or "？" in t:
        return "unclear"          # 問句既不是確認也不是拒絕
    # (a) 整句去除標點與語助詞後完全等於短確認／否定詞
    t_norm = t.strip("!！。．.~～嘛呀啦喔哦 ，,、")   # 「了」「吧」不剝：算了／不用了／好吧另列為完整詞
    if t_norm in _DENY_EXACT:
        return "no"
    if t_norm in _AFFIRM_EXACT:
        return "yes"
    t_scan = t
    for pattern in _AB_QUESTION_PATTERNS:
        t_scan = t_scan.replace(pattern, "")   # 長樣式排前面，避免被部分吃掉
    # 2026-11-02 審查修正 D（A1）：未來／意向句（「我明天要施肥」「等一下再記」）是新訊息，不是確認
    if any(m in t_scan for m in _FUTURE_INTENT_MARKERS):
        return "unclear"
    own_words = _EVENT_WORDS_BY_TYPE.get(event_type)
    if own_words is None:
        mentions_event = any(w in t_scan for w in _EVENT_WORDS)
    else:
        # 2026-11-02 審查修正 D（A1）：提到「別種事件」而沒提本事件 → 不是在回覆這筆待確認，unclear
        other_words = [w for k, ws in _EVENT_WORDS_BY_TYPE.items() if k != event_type for w in ws]
        mentions_own = any(w in t_scan for w in own_words)
        if not mentions_own and any(w in t_scan for w in other_words):
            return "unclear"
        mentions_event = mentions_own or any(w in t_scan for w in _GENERIC_EVENT_WORDS)
    # (b) 指向事件 ＋ 否定詞 → 取消（否定優先）；明確取消措辭（不用記／別記…）亦同
    if any(w in t_scan for w in _DENY_PHRASES):
        return "no"
    if mentions_event and any(w in t_scan for w in _DENY_WORDS):
        return "no"
    # (c) 明確登記措辭 → 肯定（「幫我記」「記下來」「確認登記」本身就指向登記）
    if any(w in t_scan for w in _AFFIRM_PHRASES) and not any(w in t_scan for w in _DENY_WORDS):
        return "yes"
    # (b) 指向事件 ＋ 確認詞 → 肯定（「對，記下來」「確認要記錄空心菜」「收成好」）
    if mentions_event and any(w in t_scan for w in _AFFIRM_WORDS):
        return "yes"
    # 其餘（一般追問、閒聊、與事件無關的指示）一律不明確：照常進 AI 對話，事件保留
    return "unclear"
