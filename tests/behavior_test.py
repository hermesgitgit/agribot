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
# 3. 舊版控制標記：只清不套用（strip_legacy_markers）＋ 無人監看輪次只有唯讀工具
#    （2026-10-31：[SET_*] 解析器拆掉，狀態只能經工具呼叫改；定時／哨兵／橋接用 READONLY_TOOLS）
# ======================================================================
def test_threshold_guard():
    section("strip_legacy_markers（舊標記只清不套用）")
    from agent.guard import strip_legacy_markers
    from storage.state import load_state
    no_marker = "今天土壤偏乾，建議傍晚補水。"
    check("無標記時原文不變", strip_legacy_markers(no_marker) == no_marker)
    before = load_state()
    # 合規的標記也一樣不套用——這條路已經不存在，只有工具呼叫能改狀態
    legit = "建議 [SET_THRESHOLD: dry=22, wet=38, state=mature] 並 [SET_CROP: name=番茄] 請注意"
    out = strip_legacy_markers(legit)
    check("標記全部被清除", "[SET_" not in out, f"(實得 {out})")
    check("清除後保留其餘文字", "建議" in out and "請注意" in out)
    after = load_state()
    check("合規標記也不改 state（門檻／作物原封不動）",
          (after.get("dry_threshold"), after.get("wet_threshold"), after.get("crop_name"))
          == (before.get("dry_threshold"), before.get("wet_threshold"), before.get("crop_name")))


def test_readonly_toolset():
    section("READONLY_TOOLS（無人監看輪次的工具組不含任何寫入工具）")
    from agent.tools import AGENT_TOOLS, READONLY_TOOLS
    names = {t.__name__ for t in READONLY_TOOLS}
    write_tools = {"tool_set_thresholds", "tool_set_crop", "tool_track_crop", "tool_finish_crop",
                   "tool_set_target_gdd", "tool_start_crop_cycle", "tool_record_harvest_event",
                   "tool_record_fertilizer_event", "tool_record_prediction", "tool_record_visual_assessment"}
    check("唯讀組不含任何寫入／登記工具", not (names & write_tools), f"(混入 {names & write_tools})")
    check("唯讀組是完整工具組的子集", names <= {t.__name__ for t in AGENT_TOOLS})
    check("查詢類工具都在唯讀組", {"tool_get_realtime_sensor_data", "tool_get_weather_forecast",
                                   "tool_query_daily_summaries", "tool_get_garden_status",
                                   "tool_search_agri_knowledge", "tool_assess_disease_risk"} <= names)
    import agent.session as S
    ro_cfg_names = {t.__name__ for t in S._READONLY_CONFIG.tools}
    check("session 的唯讀設定就是 READONLY_TOOLS", ro_cfg_names == names)
    import inspect
    src = inspect.getsource(S.generate_oneshot_with_retry)
    check("一次性呼叫（定時推播／哨兵）用唯讀設定", "_READONLY_CONFIG" in src and "_AGENT_CONFIG" not in src)
    check("橋接唯讀 session 與主人 session 是不同的 key",
          S._session_key(123, True) != S._session_key(123, False))
    import inspect as _i
    from bridge import server as B
    check("橋接 /ask 走 handle_message(readonly=True)", "readonly=True" in _i.getsource(B._ask))


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
def test_crop_key_divergence():
    section("作物鍵分歧：state 存自訂名、查詢用標準名，必須仍找得到")
    from storage.state import find_tracked_crop
    # 實際踩到的情境：龍鬚菜在被加進 CROP_GDD_DATABASE 之前就以自訂名註冊，
    # state 裡是 '龍鬚菜'，但之後 match_crop_key('龍鬚菜') 回 '龍鬚菜 (Chayote Shoot)'
    # → /crops 列得出來、/crop_done 與 tool_finish_crop 卻回報查無此作物，停不掉。
    legacy = {"龍鬚菜": {"accumulated_gdd": 120.0, "active": True}}
    check("state 存自訂名，用同名查得到", find_tracked_crop(legacy, "龍鬚菜") == "龍鬚菜")
    check("state 存自訂名，用標準名也查得到",
          find_tracked_crop(legacy, "龍鬚菜 (Chayote Shoot)") == "龍鬚菜")
    # 反向：state 存標準名，使用者只講短名
    canon = {"空心菜 (Water Spinach)": {"accumulated_gdd": 50.0, "active": True}}
    check("state 存標準名，用短名查得到",
          find_tracked_crop(canon, "空心菜") == "空心菜 (Water Spinach)")
    check("查不存在的作物回 None", find_tracked_crop(canon, "草莓") is None)
    check("空字典回 None", find_tracked_crop({}, "空心菜") is None)
    check("空名稱回 None", find_tracked_crop(canon, "") is None)


def test_confirmation_ab_questions():
    section("確認語意：中文正反問句不得被當成取消")
    from agent.pending import classify_confirmation as cc
    # 使用者在確認窗內問最自然的後續問題，過去會被判成「明確拒絕」：
    # 待確認的收成被丟棄、而且 handler 提前 return 把問題本身也吞掉。
    check("『那接下來要不要追肥？』→ unclear", cc("那接下來要不要追肥？") == "unclear")
    check("『是不是該收成了』→ unclear", cc("是不是該收成了") == "unclear")
    check("『用不用施肥』→ unclear", cc("用不用施肥") == "unclear")
    check("『需不需要澆水？』→ unclear", cc("需不需要澆水？") == "unclear")
    # 真正的拒絕仍要判成 no
    check("『不用了』→ no", cc("不用了") == "no")
    check("『先不要記錄』→ no", cc("先不要記錄") == "no")
    check("『算了』→ no", cc("算了") == "no")
    # 肯定側不受影響
    check("『好』→ yes", cc("好") == "yes")
    check("『幫我記』→ yes", cc("幫我記") == "yes")


def test_capture_mode_blocks_pending():
    section("橋接擷取模式：不得建立會落檔的待確認事件")
    from tg.api import start_capture, stop_capture
    from agent.pending import _set_pending_event, clear_pending_event
    # 擷取模式的回覆是回給 HTTP 呼叫端的，「若不用請喊停」擁有者看不到，
    # 10 分鐘後就被 watchdog 當默認同意寫進不可逆記錄。
    token = start_capture()
    blocked = _set_pending_event(999001, "harvest", "x") is False
    stop_capture(token)
    check("擷取模式下建立待確認事件被拒絕", blocked)
    ok = _set_pending_event(999002, "harvest", "x") is True
    clear_pending_event(999002)
    check("一般模式仍可建立", ok)


def test_prompts_helpers():
    section("prompts 輔助（濕度解析／葉菜判定／高濕時數）")
    from agent.prompts import _parse_pct, _has_leafy_crop, _high_humidity_hours
    check("_parse_pct '99%' → 99.0", _parse_pct("99%") == 99.0)
    check("_parse_pct '無資訊' → None", _parse_pct("無資訊") is None)
    check("_parse_pct '22.5' → 22.5", _parse_pct("22.5") == 22.5)
    # ⚠️ 真實格式：scrapers/agri.py 的 fmt() 必定產出 "22.0 ℃"／"99 %"。
    # 這幾則以前全部缺席，導致「氣溫永遠解析失敗、病害預警整條死掉」逃過測試。
    check("_parse_pct '25.3 ℃' → 25.3", _parse_pct("25.3 ℃") == 25.3)
    check("_parse_pct '-2.0 ℃' → -2.0", _parse_pct("-2.0 ℃") == -2.0)
    check("_parse_pct '99.0 %' → 99.0", _parse_pct("99.0 %") == 99.0)
    check("_parse_pct '0.20 ds/m' → 0.2", _parse_pct("0.20 ds/m") == 0.2)
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


def test_cwa_alerts():
    section("CWA 警特報（parse/format/has_severe_weather 連動）")
    from scrapers.cwa import parse_cwa_alerts, format_cwa_alerts, get_cwa_alerts_report
    from agent.prompts import has_severe_weather
    hz = {"records": {"location": [{"locationName": "臺北市", "hazardConditions": {"hazards": [
        {"info": {"phenomena": "海上陸上颱風", "significance": "警報"},
         "validTime": {"startTime": "2026-10-23 18:15:00", "endTime": "2026-10-23 22:15:00"}},
        {"info": {"phenomena": "豪雨", "significance": "特報"},
         "validTime": {"startTime": "2026-10-23 09:40:00", "endTime": "2026-10-24 09:40:00"}},
        {"info": {"phenomena": "", "significance": "特報"}, "validTime": {}},
    ]}}]}}
    ty = {"records": {"TropicalCyclones": {"TropicalCyclone": [
        {"CwaTyNo": 29, "TyphoonName": "CHAMPI", "CwaTyphoonName": "薔琵",
         "AnalysisData": {"Fix": [
             {"DateTime": "2026-10-14T08:00:00+08:00", "MaxWindSpeed": 12},
             {"DateTime": "2026-10-23T17:00:00+08:00", "CoordinateLatitude": 24.2,
              "CoordinateLongitude": 122.6, "MaxWindSpeed": 43, "MaxGustSpeed": 53,
              "Pressure": 945, "MovingDirection": "W", "MovingSpeed": 7}]}}]}}}
    a = parse_cwa_alerts(hz, ty)
    check("特報解析：空 phenomena 被略過、留 2 筆", len(a["hazards"]) == 2, str(a["hazards"]))
    check("特報時間格式化", a["hazards"][0]["start"] == "10/23 18:15", a["hazards"][0]["start"])
    check("颱風取最新一筆定位", a["typhoons"][0]["wind"] == 43 and a["typhoons"][0]["time"] == "10/23 17:00", str(a["typhoons"]))
    txt = format_cwa_alerts(a)
    check("文字含颱風警報與豪雨特報", "海上陸上颱風警報" in txt and "豪雨特報" in txt, txt)
    check("文字含颱風現況", "薔琵" in txt and "945" in txt, txt)
    check("has_severe_weather 對警示文字為 True", has_severe_weather(txt))
    none_txt = format_cwa_alerts({"hazards": [], "typhoons": []})
    check("無警示文案不觸發 has_severe_weather", not has_severe_weather(none_txt), none_txt)
    check("空 JSON 容忍", parse_cwa_alerts({}, {}) == {"hazards": [], "typhoons": []})
    import scrapers.cwa as C
    old = C.CWA_API_KEY
    try:
        C.CWA_API_KEY = ""
        C._ALERT_CACHE.update(ts=0.0, data=None)
        rep = get_cwa_alerts_report()
        check("沒金鑰→誠實說查詢失敗", "查詢失敗" in rep, rep)
        check("查詢失敗文案不觸發 has_severe_weather（防哨兵誤推）", not has_severe_weather(rep), rep)
    finally:
        C.CWA_API_KEY = old
        C._ALERT_CACHE.update(ts=0.0, data=None)


def test_report_resilience():
    section("generate_report_resilient（帶工具崩潰→無工具重產）")
    from agent.resilience import generate_report_resilient

    class Resp:
        def __init__(self, t):
            self.text = t

    def boom(p):
        raise KeyError("record_prediction")  # 模擬模型亂叫工具名 → AFC KeyError

    # 1) 帶工具成功 → 直接用它、不退無工具
    r = generate_report_resilient("p",
                                  lambda p: Resp("帶工具報告"),
                                  lambda p: Resp("無工具報告"),
                                  lambda e: False)
    check("帶工具成功時用帶工具結果", r.text == "帶工具報告")

    # 2) 帶工具非壅塞崩潰（KeyError）→ 退到無工具重產（不整輪陣亡）
    r2 = generate_report_resilient("p", boom, lambda p: Resp("無工具報告"), lambda e: False)
    check("帶工具崩潰(非壅塞)→退無工具重產", r2.text == "無工具報告")

    # 3) 帶工具壅塞崩潰 → 往外拋（交備援），不被吞掉
    raised = False
    try:
        generate_report_resilient("p", boom, lambda p: Resp("不該用到"), lambda e: True)
    except KeyError:
        raised = True
    check("帶工具壅塞崩潰→往外拋交備援", raised)


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


def test_harvest_calibration():
    section("播種起算 /sow、自訂目標 /target、首採積溫校準")
    import datetime
    import sqlite3
    import tempfile
    import storage.state as S
    import storage.harvest as H
    from science.cadence import maturity_calibration, median
    from config import TZ_TAIPEI

    # 純邏輯
    check("median 奇數/偶數", median([3, 1, 2]) == 2 and median([1, 4]) == 2.5)
    t, sug = maturity_calibration([], 300)
    check("無樣本→不建議", t == "" and sug is None)
    t, sug = maturity_calibration([300.0], 350)
    check("單一樣本→只提示再多一輪", "再多一輪" in t and sug is None, t)
    t, sug = maturity_calibration([320.0, 340.0, 330.0], 280, "空心菜 (Water Spinach)")
    check("三輪中位數 330、差 18%→建議 /target 空心菜 330", sug == 330 and "/target 空心菜 330" in t, t)
    t, sug = maturity_calibration([300.0, 310.0], 305)
    check("差在 10% 內→相符不建議", "相符" in t and sug is None, t)

    tmp = tempfile.mkdtemp()
    old_state, old_db, old_ready = S.STATE_FILE, H.DB_FILE, H.HARVEST_TABLE_READY
    S.STATE_FILE = os.path.join(tmp, "state.json")
    H.DB_FILE = os.path.join(tmp, "agri.db")
    H.HARVEST_TABLE_READY = False
    try:
        crop = "空心菜 (Water Spinach)"
        S.update_state(lambda st: st["crops"].update({crop: {"accumulated_gdd": 450.0, "last_gdd_date": "2026-10-01", "active": True}}))
        key, old_acc, old_start = S.start_crop_cycle("空心菜", "第二畦")
        st = S.load_state()
        e = st["crops"][key]
        today = datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
        check("start_crop_cycle 對到既有鍵、歸零、記起算日", key == crop and e["accumulated_gdd"] == 0.0 and e["cycle_start_date"] == today, str(e))
        check("上一輪累計存進 prev_cycle", old_acc == 450.0 and e["prev_cycle"]["accumulated_gdd"] == 450.0)
        yday = (datetime.datetime.now(TZ_TAIPEI) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        check("last_gdd_date 設為昨天（今晚只結算今天）", e["last_gdd_date"] == yday and e["active"] is True)

        check("crop_target_gdd 預設取資料庫 300", S.crop_target_gdd(st, crop) == 300.0)
        k2, eff, found = S.set_crop_target("空心菜", 350)
        check("set_crop_target 自訂 350", found and eff == 350.0 and k2 == crop)
        check("crop_target_gdd 讀到自訂", S.crop_target_gdd(S.load_state(), crop) == 350.0)
        k3, eff3, found3 = S.set_crop_target("空心菜", None)
        check("reset 回到 300", found3 and eff3 == 300.0)
        _, _, found4 = S.set_crop_target("火龍果", 500)
        check("不存在的作物→found False", found4 is False)

        # 先塞一輪舊紀錄（8 月那輪首採 320），再模擬本輪累計到 280 首採
        H._ensure_harvest_table()
        c = sqlite3.connect(H.DB_FILE)
        c.execute("INSERT INTO harvest_records (harvest_date, crop_name, is_first_of_cycle, accumulated_gdd_snapshot, note, cycle_start_date) VALUES (?,?,?,?,?,?)",
                  ("2026-08-25", crop, 1, 320.0, "", "2026-08-01"))
        c.commit(); c.close()
        S.update_state(lambda st: st["crops"][crop].update({"accumulated_gdd": 280.0}))
        msg = H.record_harvest("", crop_hint="空心菜")
        check("首採訊息含本輪起算與累計 280", f"自 {today} 播種起累計 280.0" in msg, msg)
        check("兩輪樣本→中位數 300 與目標相符", "2 輪" in msg and "相符" in msg, msg)
        check("舊的 >45 天空窗視為新一輪基準點", "第一次割收" in msg, msg)
        dup = H.record_harvest("", crop_hint="空心菜")
        check("同日重複登記被擋", "不重複登記" in dup, dup)
        summ = H.get_harvest_cadence_summary(crop)
        check("harvest_stats 含目標來源與起算日", "成熟目標積溫：300.0" in summ and today in summ, summ)
        check("harvest_stats 含校準行", "首採積溫中位數約 300" in summ, summ)

        # 沒用 /sow 的作物：割收只給提示，不算樣本
        S.update_state(lambda st: st["crops"].update({"秋葵 (Okra)": {"accumulated_gdd": 900.0, "last_gdd_date": "", "active": True}}))
        msg2 = H.record_harvest("", crop_hint="秋葵")
        check("無起算日→提示用 /sow、不出校準", "提示：播種" in msg2 and "本輪自" not in msg2, msg2)
        check("無起算日的紀錄不進樣本", H._maturity_samples("秋葵 (Okra)") == [])
    finally:
        S.STATE_FILE, H.DB_FILE, H.HARVEST_TABLE_READY = old_state, old_db, old_ready


def main():
    print("===== agribot 行為回歸測試 =====")
    test_classify_confirmation()
    test_strip_links()
    test_threshold_guard()
    test_disease_two_pathway()
    test_crop_key_divergence()
    test_confirmation_ab_questions()
    test_capture_mode_blocks_pending()
    test_prompts_helpers()
    test_science_sanity()
    test_disease_knowledge_link()
    test_recent_rain()
    test_kb_context()
    test_cwa_alerts()
    test_report_resilience()
    test_harvest_calibration()
    test_readonly_toolset()
    print_model_checklist()
    print(f"\n===== 結果：{_PASS} 通過 / {_FAIL} 失敗 =====")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
