# ======================================================================
# 知識庫離線建置腳本（在本機跑，不進容器）
# ======================================================================
# 輸入：moa_manifest.json（書目）＋ moa_pdfs/（下載好的 PDF）
# 輸出：knowledge.db —— books / chunks 兩張表 + chunks_fts（FTS5 bigram 索引）
# 用法：python3 scripts/build_knowledge_db.py <manifest.json> <pdf目錄> <輸出db>
# 完成後把 knowledge.db 放到 NAS 的 data/ 資料夾即可，免重建映像檔。
import json
import os
import re
import sqlite3
import sys

from pypdf import PdfReader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage.textseg import to_bigrams  # noqa: E402  零依賴模組，不會觸發 config 的環境變數檢查

CHUNK_CHARS = 700   # 每塊目標字數
OVERLAP = 80        # 相鄰塊重疊，避免關鍵句被切斷
MIN_CJK_RATIO = 0.15  # 頁面中文比例低於此值且字數少 → 視為掃描/亂碼頁


def clean(text: str) -> str:
    text = re.sub(r'[ \t　]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()


def cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = len(re.findall(r'[一-鿿]', s))
    return cjk / len(s)


def chunk_pages(pages):
    """pages: [(page_no, text)] → [(page_no, chunk_text)]，跨頁累積至目標字數。"""
    chunks, buf, buf_page = [], "", None
    for pno, txt in pages:
        if buf_page is None:
            buf_page = pno
        buf += ("\n" if buf else "") + txt
        while len(buf) >= CHUNK_CHARS:
            chunks.append((buf_page, buf[:CHUNK_CHARS]))
            buf = buf[CHUNK_CHARS - OVERLAP:]
            buf_page = pno
    if len(buf.strip()) >= 60:  # 太短的尾巴沒檢索價值
        chunks.append((buf_page, buf))
    return chunks


def main(manifest_path, pdf_dir, db_path):
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE books (book_id TEXT, fid TEXT, title TEXT, pages INT, chunks INT,
                            status TEXT, PRIMARY KEY (book_id, fid));
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, book_id TEXT, title TEXT, page INT, text TEXT);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(seg, content='', tokenize='unicode61');
    """)

    total_chunks = 0
    garbled = []
    for b in manifest:
        for f in b["files"]:
            path = os.path.join(pdf_dir, f"{b['book_id']}_{f['fid']}.pdf")
            if not os.path.exists(path):
                conn.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?,?,?)",
                             (b["book_id"], f["fid"], b["title"], 0, 0, "missing"))
                continue
            try:
                reader = PdfReader(path)
                pages = []
                for i, pg in enumerate(reader.pages, 1):
                    try:
                        t = clean(pg.extract_text() or "")
                    except Exception:
                        t = ""
                    if len(t) >= 40 and cjk_ratio(t) >= MIN_CJK_RATIO:
                        pages.append((i, t))
                status = "ok"
                if not pages:
                    status = "no_text"  # 掃描檔或抽取失敗，待 OCR
                    garbled.append(b["title"])
                cks = chunk_pages(pages)
                for pno, txt in cks:
                    cur = conn.execute(
                        "INSERT INTO chunks (book_id, title, page, text) VALUES (?,?,?,?)",
                        (b["book_id"], b["title"], pno, txt))
                    conn.execute("INSERT INTO chunks_fts (rowid, seg) VALUES (?, ?)",
                                 (cur.lastrowid, to_bigrams(txt)))
                conn.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?,?,?)",
                             (b["book_id"], f["fid"], b["title"], len(reader.pages), len(cks), status))
                total_chunks += len(cks)
                print(f"{'OK ' if status == 'ok' else '⚠️ '} {b['title'][:36]:38s} {len(reader.pages):4d}頁 → {len(cks):4d}塊")
            except Exception as e:
                conn.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?,?,?)",
                             (b["book_id"], f["fid"], b["title"], 0, 0, f"error:{e}"))
                print(f"❌ {b['title'][:36]}: {e}")
        conn.commit()

    conn.execute("INSERT INTO chunks_fts (chunks_fts) VALUES ('optimize')")
    conn.commit()
    n_books = conn.execute("SELECT COUNT(DISTINCT book_id) FROM chunks").fetchone()[0]
    conn.close()
    print(f"\n完成：{n_books} 本書、{total_chunks} 個檢索塊 → {db_path}"
          f"（{os.path.getsize(db_path) / 1048576:.1f} MB）")
    if garbled:
        print(f"無法抽取文字（掃描檔，留待 OCR）：{len(garbled)} 本")
        for t in garbled:
            print("  -", t)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
