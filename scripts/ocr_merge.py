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
# 掃描檔 OCR 並併入既有 knowledge.db
# ======================================================================
# 對 knowledge.db 中 status='no_text' 的書，逐本以 Swift Vision OCR 工具
# 抽出文字，沿用 build_knowledge_db 的切塊與 bigram 索引邏輯併入同一資料庫。
# 用法：python3 scripts/ocr_merge.py <manifest.json> <pdf目錄> <knowledge.db>
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage.textseg import to_bigrams  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_BIN = os.path.join(HERE, "ocr_pdf")

CHUNK_CHARS = 700
OVERLAP = 80
MIN_CJK_RATIO = 0.15


def clean(t):
    t = re.sub(r'[ \t　]+', ' ', t)
    t = re.sub(r'\n{2,}', '\n', t)
    return t.strip()


def cjk_ratio(s):
    return len(re.findall(r'[一-鿿]', s)) / len(s) if s else 0.0


def chunk_pages(pages):
    chunks, buf, buf_page = [], "", None
    for pno, txt in pages:
        if buf_page is None:
            buf_page = pno
        buf += ("\n" if buf else "") + txt
        while len(buf) >= CHUNK_CHARS:
            chunks.append((buf_page, buf[:CHUNK_CHARS]))
            buf = buf[CHUNK_CHARS - OVERLAP:]
            buf_page = pno
    if len(buf.strip()) >= 60:
        chunks.append((buf_page, buf))
    return chunks


def parse_ocr(raw):
    """Swift 工具以 \\x0c<頁碼>\\x0c\\n 標記每頁起點，解析回 [(page_no, text)]。"""
    pages = []
    # 以換頁符分隔的頁碼標記精確切頁（不會誤切內文中的數字）
    segs = re.split("\x0c(\\d+)\x0c\n", raw)
    # segs = ['', '1', text1, '2', text2, ...]
    for i in range(1, len(segs) - 1, 2):
        pno = int(segs[i])
        txt = clean(segs[i + 1])
        if len(txt) >= 40 and cjk_ratio(txt) >= MIN_CJK_RATIO:
            pages.append((pno, txt))
    return pages


def main(manifest_path, pdf_dir, db_path):
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    title_of = {(b["book_id"], f["fid"]): b["title"] for b in manifest for f in b["files"]}

    conn = sqlite3.connect(db_path)
    todo = conn.execute("SELECT book_id, fid FROM books WHERE status='no_text'").fetchall()
    print(f"待 OCR：{len(todo)} 本")

    ok = empty = 0
    for n, (bid, fid) in enumerate(todo, 1):
        title = title_of.get((bid, fid), bid)
        path = os.path.join(pdf_dir, f"{bid}_{fid}.pdf")
        if not os.path.exists(path):
            continue
        try:
            raw = subprocess.run([OCR_BIN, path], capture_output=True, timeout=1800).stdout.decode("utf-8", "replace")
        except Exception as e:
            print(f"❌ {title[:34]}: {e}", flush=True)
            continue
        pages = parse_ocr(raw)
        cks = chunk_pages(pages)
        for pno, txt in cks:
            cur = conn.execute("INSERT INTO chunks (book_id, title, page, text) VALUES (?,?,?,?)",
                               (bid, title, pno, txt))
            conn.execute("INSERT INTO chunks_fts (rowid, seg) VALUES (?, ?)", (cur.lastrowid, to_bigrams(txt)))
        status = "ocr" if cks else "ocr_empty"
        conn.execute("UPDATE books SET chunks=?, status=? WHERE book_id=? AND fid=?",
                     (len(cks), status, bid, fid))
        conn.commit()
        if cks:
            ok += 1
        else:
            empty += 1
        print(f"[{n}/{len(todo)}] {'OK ' if cks else '空 '} {title[:34]:36s} → {len(cks)} 塊", flush=True)

    conn.execute("INSERT INTO chunks_fts (chunks_fts) VALUES ('optimize')")
    conn.commit()
    tot = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    nbooks = conn.execute("SELECT COUNT(DISTINCT book_id) FROM chunks").fetchone()[0]
    conn.close()
    print(f"\nOCR 併入完成：成功 {ok} 本、無文字 {empty} 本。"
          f"全庫現有 {nbooks} 本、{tot} 塊（{os.path.getsize(db_path)/1048576:.1f} MB）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
