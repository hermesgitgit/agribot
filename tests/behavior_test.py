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
# 行為回歸測試 (Behavior Regression Tests)
# ======================================================================
# 目的：把那些「靠 SYSTEM_INSTRUCTION / 防護欄管教模型」的關鍵行為，用程式
# 鎖成可驗的測試——這樣日後要精簡那份巨大的系統指令時，有個東西能告訴你
# 「有沒有改壞既有行為」，不必再憑感覺、提心吊膽。
#
# 兩種測試分開：
#   1. 純邏輯（本檔大部分）：不需要真的問 Gemini、不花錢、可隨時／CI 跑。
#   2. 需要問模型才驗得出來的（檔尾清單）：先列成手動檢查項，要測時照單跑。
#
# 跑法：  python3 tests/behavior_test.py
# 失敗時 exit code 非 0，方便接 CI。
import os
import sys

# 讓 tests/ 底下的腳本能 import 專案根目錄的模組（agent / science / storage…）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 純邏輯測試不需要真金鑰，補上假的讓模組能 import（不會真的連線）
for _k, _v in {
    "GEMINI_API_KEY": "test", "AGRI_API_KEY": "test", "AGRI_SUID": "test",
    "TELEGRAM_TOKEN": "test", "TELEGRAM_CHAT_ID": "123", "CWA_API_KEY": "test",
}.items():
    os.environ.setdefault(_k, _v)

_PASS = 0
_FAIL = 0


def check(name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  ✓ {name}")
    else:
        _FAIL += 1
        print(f"  ✗ {name}   {detail}")


def section(title):
    print(f"\n— {title} —")


# ======================================================================
# 1. 確認語意判斷 classify_confirmation
#    （收成/施肥確認流程的核心；含當初「確認要記錄空心菜」被漏判的回歸）
# ======================================================================
def test_classify_confirmation():
    section("classify_confirmation（確認語意）")
    from agent.pending import classify_confirmation as cc
    cases = [
        ("好", "yes"),
        ("好的", "yes"),
        ("可以", "yes"),
        ("麻煩你幫我記", "yes"),
        ("確認要記錄空心菜", "yes"),   # 回歸：帶作物名的明確確認
        ("確定要登記", "yes"),
        ("要記空心菜", "yes"),
        ("不用", "no"),
        ("先不要記錄", "no"),
        ("算了", "no"),
        ("對了，明天會下雨嗎", "unclear"),   # 不可因含「對」誤判成 yes
        ("可以幫我看葉子嗎？", "unclear"),   # 問句不算確認
        ("", "unclear"),
    ]
    for text, expect in cases:
        got = cc(text)
        check(f"'{text}' → {expect}", got == expect, f"(實得 {got})")


# ======================================================================
# 2. 連結攔截 strip_links（「回覆禁止網址」改由程式強制，非靠模型自律）
# ======================================================================
def test_strip_links():
    section("strip_links（連結攔截）")
    from agent.guard import strip_links
    r1 = strip_links("詳見 https://example.com/x 謝謝")
    check("移除 https 連結", "https://" not in r1 and "連結已由系統移除" in r1, f"(實得 {r1})")
    r2 = strip_links("看 www.test.org 這裡")
    check("移除 www 連結", "www.test.org" not in r2, f"(實得 {r2})")
    r3 = strip_links("純文字沒有連結")
    check("無連結時原文不變", r3 == "純文字沒有連結")


# ======================================================================
# 3. 門檻指令防護 apply_threshold_command
#    （只驗純文字面：標記一定被清除、不合規範圍被拒；不觸碰 state）
# ======================================================================
def test_threshold_guard():
    section("apply_threshold_command（門檻防護，純文字面）")
    from agent.guard import apply_threshold_command
    no_marker = "今天土壤偏乾，建議傍晚補水。"
    check("無標記時原文不變", apply_threshold_command(no_marker) == no_marker)
    bad = "建議 [SET_THRESHOLD: dry=90, wet=50, state=mature] 請注意"
    out = apply_threshold_command(bad)  # dry>wet 不合規：清標記、拒套用、不寫 state
    check("不合規標記被清除", "[SET_THRESHOLD" not in out, f"(實得 {out})")
    check("清除後保留其餘文字", "建議" in out and "請注意" in out)


# ======================================================================
# 4. 病害雙路徑 assess_disease_risk
#    （葉面潮濕 vs 雨水濺潑：霧不該觸發軟腐病、雨+暖才該）
# ======================================================================
def test_disease_two_pathway():
    section("assess_disease_risk（病害雙路徑）")
    from science.disease import assess_disease_risk as A
    fog = A(air_temp=22, air_humidity=99, high_humidity_hours=14, recent_rain_mm=0)
    check("濃霧無雨：不列軟腐/炭疽",
          "軟腐病" not in fog["diseases"] and "炭疽病" not in fog["diseases"], f"({fog['diseases']})")
    check("濃霧高濕：風險為高", fog["level"] == "高", f"({fog['level']})")
    rain = A(air_temp=26, air_humidity=99, high_humidity_hours=14, recent_rain_mm=15)
    check("雨+暖：列入軟腐/炭疽",
          "軟腐病" in rain["diseases"] and "炭疽病" in rain["diseases"], f"({rain['diseases']})")
    cold = A(air_temp=12, air_humidity=92, high_humidity_hours=8, recent_rain_mm=8)
    check("冷雨：太冷不列濺潑型", "軟腐病" not in cold["diseases"], f"({cold['diseases']})")
    miss = A(air_temp=None, air_humidity=99, high_humidity_hours=14)
    check("缺氣溫：回未知不妄判", miss["level"] == "未知", f"({miss['level']})")


# ======================================================================
# 5. prompts 病害輸入的小工具
# ======================================================================
def test_prompts_helpers():
    section("prompts 輔助（濕度解析／葉菜判定／高濕時數）")
    from agent.prompts import _parse_pct, _has_leafy_crop, _high_humidity_hours
    check("_parse_pct '99%' → 99.0", _parse_pct("99%") == 99.0)
    check("_parse_pct '無資訊' → None", _parse_pct("無資訊") is None)
    check("_parse_pct '22.5' → 22.5", _parse_pct("22.5") == 22.5)
    check("葉菜判定：空心菜→True", _has_leafy_crop(["空心菜"]) is True)
    check("葉菜判定：純番茄→False", _has_leafy_crop(["番茄 (Tomato)"]) is False)
    check("葉菜判定：番茄+萵苣→True", _has_leafy_crop(["番茄 (Tomato)", "萵苣"]) is True)

    import datetime
    from config import TZ_TAIPEI
    now = datetime.datetime.now(TZ_TAIPEI)
    recs = []
    for i in range(30):
        t = now - datetime.timedelta(hours=i)
        recs.append({"timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                     "air_humidity": "99" if i < 14 else "70", "air_temperature": "22"})
    check("近24h高濕時數＝14", _high_humidity_hours(recs) == 14, f"(實得 {_high_humidity_hours(recs)})")


# ======================================================================
# 6. 科學層基本健全性（GDD / 生長階段）
# ======================================================================
def test_science_sanity():
    section("science 基本健全性")
    from science.gdd import gdd_from_minmax
    from science.water import growth_stage, crop_kc
    check("GDD 暖日為正", gdd_from_minmax(20, 30, 10, 35) > 0)
    check("GDD 冷日為 0（皆低於基溫）", gdd_from_minmax(5, 8, 10, 35) == 0)
    check("生長階段 幼苗≠成熟", growth_stage(0.0) != growth_stage(0.95))
    check("作物係數 Kc 為正", crop_kc("空心菜 (Water Spinach)", 0.5) > 0)


# ======================================================================
# 7. 病害→知識庫「嚴格命中」連動（需臨時知識庫；驗防誤植的誠實守則）
# ======================================================================
def _build_temp_knowledge_db():
    import sqlite3
    import tempfile
    from storage.textseg import to_bigrams
    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, book_id TEXT, title TEXT, page INT, text TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(seg, content='', tokenize='unicode61');")
    docs = [("蔬菜病蟲害", 88, "露菌病防治：摘除病葉，加強通風與排水。"),
            ("葉菜栽培手冊", 42, "白銹病防治：清除殘體，避免密植，雨後注意排水。")]
    for i, (t, pg, tx) in enumerate(docs, 1):
        c.execute("INSERT INTO chunks VALUES (?,?,?,?,?)", (i, "b", t, pg, tx))
        c.execute("INSERT INTO chunks_fts(rowid, seg) VALUES (?,?)", (i, to_bigrams(tx)))
    c.commit()
    c.close()
    return path


def test_disease_knowledge_link():
    section("disease_control_excerpts（病害防治嚴格命中）")
    import storage.knowledge as K
    from agent import prompts as P
    db = _build_temp_knowledge_db()
    K.KNOWLEDGE_DB_FILE = db
    blk = P.disease_control_excerpts(["露菌病", "白銹病"])
    check("有庫病害各自命中", "露菌病" in blk and "白銹病" in blk and "官方防治參考" in blk)
    nope = P.disease_control_excerpts(["灰黴病"])
    check("無庫病害不誤植別病、誠實退回",
          "通用預防原則" in nope and "露菌病" not in nope, f"({nope[:40]})")
    os.unlink(db)


# ======================================================================
# 8. 雨量近 24h 視窗 recent_rain_mm（需臨時 DB）
# ======================================================================
def test_recent_rain():
    section("recent_rain_mm（近24h雨量視窗）")
    import datetime
    import sqlite3
    import tempfile
    import storage.rain as R
    from config import TZ_TAIPEI
    db = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE rain_log (ts TEXT PRIMARY KEY, precip_mm REAL)")
    now = datetime.datetime.now(TZ_TAIPEI)
    for hours_ago, mm in [(25, 99.0), (10, 8.0), (2, 2.0)]:
        ts = (now - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO rain_log VALUES (?,?)", (ts, mm))
    c.commit()
    c.close()
    R.DB_FILE = db
    check("近24h取最大、排除25h前的99", R.recent_rain_mm(24) == 8.0, f"(實得 {R.recent_rain_mm(24)})")
    R.DB_FILE = db  # 保持指向臨時庫
    os.unlink(db)


def test_kb_context():
    section("build_kb_context（病害問題自動預取知識庫）")
    import sqlite3
    import tempfile
    import storage.knowledge as K
    from agent.prompts import build_kb_context
    from storage.textseg import to_bigrams
    db = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, book_id TEXT, title TEXT, page INT, text TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(seg, content='', tokenize='unicode61');")
    tx = "空心菜常見病害白銹病防治：避免密植、加強通風、雨後注意排水。"
    c.execute("INSERT INTO chunks VALUES (1,'b','蔬菜病蟲害',50,?)", (tx,))
    c.execute("INSERT INTO chunks_fts(rowid, seg) VALUES (1,?)", (to_bigrams(tx),))
    c.commit()
    c.close()
    K.KNOWLEDGE_DB_FILE = db
    hit = build_kb_context("空心菜最近會不會生病？要怎麼防治？")
    check("病害問題→自動注入官方節錄、含書名", "蔬菜病蟲害" in hit, f"({hit[:30]})")
    miss = build_kb_context("謝謝你，今天辛苦了")
    check("一般閒聊→不注入（回空字串）", miss == "", f"({miss[:30]})")
    os.unlink(db)


# ======================================================================
# 需要問 Gemini 才驗得出來的行為（手動清單，尚未自動化）
# 這些靠 SYSTEM_INSTRUCTION 的自由文字行為，本機純邏輯驗不出，要實際呼叫模型。
# 精簡系統指令前，挑相關項目把「傳的話」貼給 bot、對照「過關/出包」確認沒退步。
# 每項 = (代號＋說明, 要傳給 bot 的話, 過關長怎樣, 出包的徵兆)。
# ======================================================================
_MODEL_CHECKS = [
    ("M1 寒暄不該丟報告",
     "早安，今天天氣不錯",
     "自然簡短回個寒暄",
     "回了一整篇含『灌溉建議/施肥建議』的農務報告"),
    ("M2 被糾正要承認、別硬給建議",
     "你誤會了，我只是隨口講講，沒有要你分析數據給建議",
     "承認或澄清、語氣自然",
     "還是塞一堆土壤濕度/氣溫分析給你"),
    ("M3 說收成了要登記並告知可喊停",
     "我今天收成空心菜了",
     "說會幫你登記、並提醒『不用可回不用』（測完記得回『不用』取消假紀錄）",
     "沒反應，或默默記了卻沒告訴你"),
    ("M4 只是考慮施肥不該登記",
     "我在想要不要施肥，你覺得呢？",
     "給該不該施肥的建議，但不登記",
     "跳出施肥登記的確認"),
    ("M5 病害詢問要引用書名且標明預防",
     "最近這麼濕悶，菜會不會生病？要怎麼預防？",
     "給預防做法、引用《書名》、標明是預防非診斷",
     "憑空給建議沒出處，或講得像已確診某病"),
    ("M6 回覆不該出現網址",
     "可以給我一個查空心菜病害的網站連結嗎？",
     "用文字說明、不給任何連結",
     "回覆裡真的出現一個可點的網址"),
]


def print_model_checklist():
    section("需問模型的行為（手動，未自動化）")
    print("  挑你改動到的相關項目，把『傳：』那句貼給 bot，對照過關/出包：")
    for cid, msg, ok, bad in _MODEL_CHECKS:
        print(f"    {cid}")
        print(f"      傳：「{msg}」")
        print(f"      ✓ 過：{ok}")
        print(f"      ✗ 包：{bad}")


def main():
    print("===== agribot 行為回歸測試 =====")
    test_classify_confirmation()
    test_strip_links()
    test_threshold_guard()
    test_disease_two_pathway()
    test_prompts_helpers()
    test_science_sanity()
    test_disease_knowledge_link()
    test_recent_rain()
    test_kb_context()
    print_model_checklist()
    print(f"\n===== 結果：{_PASS} 通過 / {_FAIL} 失敗 =====")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
