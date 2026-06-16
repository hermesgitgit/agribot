# ======================================================================
# 農業知識庫檢索 (Agricultural Knowledge Base, RAG)
# ======================================================================
# 語料：台灣農業部「農業知識入口網」公開出版品（栽培管理、病蟲害防治、
# 土壤肥料等技術手冊），由 scripts/build_knowledge_db.py 離線建成
# knowledge.db（chunks 全文 + FTS5 bigram 索引），放置於 /app/data/ 下。
# 個人非營利研究用途，輸出一律附書名出處（符合農業部網站資料開放宣告
# 之合理使用與註明出處要求）。
# 誠實原則：知識庫未安裝或查無結果時明說，不臆造內容。
import os
import sqlite3

from config import KNOWLEDGE_DB_FILE
from logging_setup import logger
from storage.textseg import build_fts_query

_MAX_PER_BOOK = 2  # 結果多樣性：同一本書最多取幾段，避免單一書洗版


def knowledge_available() -> bool:
    return os.path.exists(KNOWLEDGE_DB_FILE)


def fetch_excerpts(query: str, k: int = 6):
    """
    低階檢索：回傳 (picked, relaxed)。
      picked  = [(title, page, text), ...]，已做「同書最多 _MAX_PER_BOOK 段」的多樣性過濾；
      relaxed = True 表示全部關鍵詞無同段命中、退階為任一關鍵詞命中。
    查無結果、未安裝或出錯一律回 ([], False)（呼叫端據此自行措辭）。
    供 search_knowledge 與病害防治自動連動（build_disease_report）共用。
    """
    query = (query or "").strip()[:80]
    if not query or not knowledge_available():
        return [], False
    conn = None
    try:
        conn = sqlite3.connect(f"file:{KNOWLEDGE_DB_FILE}?mode=ro", uri=True)
        relaxed = False
        rows = _query(conn, build_fts_query(query, require_all=True), k * 6)
        if not rows:
            relaxed = True
            rows = _query(conn, build_fts_query(query, require_all=False), k * 6)
    except Exception as e:
        logger.warning(f"⚠️ [Knowledge] 檢索失敗: {e}")
        return [], False
    finally:
        if conn is not None:
            conn.close()

    # 結果多樣性：同一本書最多 _MAX_PER_BOOK 段
    picked, per_book = [], {}
    for _, title, page, text in rows:
        if per_book.get(title, 0) >= _MAX_PER_BOOK:
            continue
        per_book[title] = per_book.get(title, 0) + 1
        picked.append((title, page, text))
        if len(picked) >= k:
            break
    return picked, relaxed


def search_knowledge(query: str, k: int = 6) -> str:
    """
    全文檢索知識庫，回傳格式化的段落與出處（書名＋頁碼）。
    先以「全部關鍵詞命中」查詢，無結果時退階為「任一關鍵詞命中」並標明。
    """
    query = (query or "").strip()[:80]
    if not query:
        return "【農業知識庫】請提供查詢關鍵詞（例：空心菜 病害）。"
    if not knowledge_available():
        return ("【農業知識庫】尚未安裝（找不到 knowledge.db）。"
                "請將建好的知識庫檔案放入資料目錄後重啟。")

    picked, relaxed = fetch_excerpts(query, k)
    if not picked:
        return (f"【農業知識庫】找不到與「{query}」相關的內容。"
                "可換更一般的關鍵詞再試（例如以作物名、病名、肥料名查詢）。")

    note = "（注意：全部關鍵詞無同段命中，以下為部分關鍵詞的結果）\n" if relaxed else ""
    lines = [f"【農業知識庫檢索 —「{query}」，{len(picked)} 段，來源為農業部出版品】", note.rstrip()]
    for title, page, text in picked:
        snippet = " ".join(text.split())
        lines.append(f"▪️《{title}》p.{page}：{snippet}")
    lines.append("（回覆使用者時請註明引用的書名。內容為出版品原文節錄，請結合現場實際狀況判斷。）")
    return "\n".join(l for l in lines if l)


def _query(conn, fts_query: str, limit: int):
    if not fts_query:
        return []
    return conn.execute(
        "SELECT c.book_id, c.title, c.page, c.text "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
        (fts_query, limit)).fetchall()
