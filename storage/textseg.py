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
# 中文全文檢索的 bigram 切詞 (CJK Bigram Segmentation)
# ======================================================================
# SQLite FTS5 的 unicode61 tokenizer 不會切中文（整句變一個 token）、
# trigram tokenizer 又匹配不到「施肥」「灌溉」這類兩字詞。
# 解法：索引與查詢兩端都先把連續的中日韓字元展開成重疊的「字元雙連」
# （例：空心菜白銹病 → 空心 心菜 菜白 白銹 銹病），英數字保持原樣，
# 再交給 unicode61 做標準 token 比對——兩字詞以上的中文詞彙皆可精確命中。
# 本模組零依賴（不 import config），知識庫建置腳本與 bot 端共用。
import re

_CJK = re.compile(r'[一-鿿㐀-䶿]+')


def to_bigrams(text: str) -> str:
    """將文字中的連續中文段展開為空格分隔的 bigram 序列；其餘內容原樣保留。"""
    def _expand(m):
        s = m.group(0)
        if len(s) == 1:
            return f" {s} "
        return " " + " ".join(s[i:i + 2] for i in range(len(s) - 1)) + " "
    return _CJK.sub(_expand, text)


def build_fts_query(query: str, require_all: bool = True) -> str:
    """
    將使用者查詢轉為 FTS5 查詢字串：中文切 bigram、各 token 以雙引號包成字面字串
    （防 FTS5 把 AND/OR/NEAR/* 等當運算子，亦防語法注入）。
    require_all=True 時以隱含 AND 連接（全部命中），False 時以 OR 連接（寬鬆退階）。
    token 內嵌的雙引號一律剝除——否則會組出不成對的引號，使整句 MATCH 語法錯誤。
    """
    quoted = []
    for t in to_bigrams(query).split():
        t = t.replace('"', '')  # 剝除內嵌引號，避免破壞字面字串的成對性
        if t:
            quoted.append(f'"{t}"')
    return (" " if require_all else " OR ").join(quoted)
