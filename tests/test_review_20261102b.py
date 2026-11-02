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
# 2026-11-02 審查修正 B 的離線回歸測試（unittest，不碰網路）
# ======================================================================
# 跑法：python3 tests/test_review_20261102b.py
# 每一項對應外部審查報告的編號：A1 確認語意、A2 感測新鮮度、A3 逾時後執行緒續跑、
# A4 落檔失敗仍回成功、A5 重播種首採、A6 切換作物重複帳、A7 恢復追蹤補算、A8 推播未送達、
# 以及照片備援的措辭。
import asyncio
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k, _v in {
    "GEMINI_API_KEY": "test", "AGRI_API_KEY": "test", "AGRI_SUID": "test",
    "TELEGRAM_TOKEN": "test", "TELEGRAM_CHAT_ID": "123", "CWA_API_KEY": "test",
}.items():
    os.environ.setdefault(_k, _v)

from config import TZ_TAIPEI  # noqa: E402


def _today():
    return datetime.datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")


def _days_ago(n):
    return (datetime.datetime.now(TZ_TAIPEI) - datetime.timedelta(days=n)).strftime("%Y-%m-%d")


class _TempState:
    """把 state.json／harvest DB 指到暫存目錄，測完還原。"""

    def __enter__(self):
        import storage.state as S
        import storage.harvest as H
        self.S, self.H = S, H
        self.tmp = tempfile.mkdtemp()
        self.old = (S.STATE_FILE, H.DB_FILE, H.HARVEST_TABLE_READY)
        S.STATE_FILE = os.path.join(self.tmp, "state.json")
        H.DB_FILE = os.path.join(self.tmp, "agri.db")
        H.HARVEST_TABLE_READY = False
        return self

    def __exit__(self, *a):
        self.S.STATE_FILE, self.H.DB_FILE, self.H.HARVEST_TABLE_READY = self.old


# ---------------------------------------------------------------- A1
class TestA1ClassifyConfirmation(unittest.TestCase):
    def test_general_followups_are_unclear(self):
        from agent.pending import classify_confirmation as cc
        # 審查報告的兩個反例：以前分別被判成 no（取消收成）與 yes（登記）
        self.assertEqual(cc("今天不用澆水了"), "unclear")
        self.assertEqual(cc("麻煩你幫我查一下天氣"), "unclear")
        self.assertEqual(cc("我要去買肥料"), "unclear")
        self.assertEqual(cc("那接下來要不要追肥？"), "unclear")

    def test_explicit_replies(self):
        from agent.pending import classify_confirmation as cc
        self.assertEqual(cc("好"), "yes")
        self.assertEqual(cc("不用"), "no")
        self.assertEqual(cc("收成不用記"), "no")
        self.assertEqual(cc("對，記下來"), "yes")
        self.assertEqual(cc("算了"), "no")
        self.assertEqual(cc("不用了"), "no")
        self.assertEqual(cc("好，不用記了"), "no")   # 否定優先
        self.assertEqual(cc("確認要記錄空心菜"), "yes")

    def test_event_type_aware_and_future_intent(self):
        # 2026-11-02 審查修正 D（A1）：外部審查第三輪重現——待確認收成時「我明天要施肥」
        # 以前被判 yes（施肥也算事件詞、「要」算確認詞），收成誤落檔且新訊息被吞。
        from agent.pending import classify_confirmation as cc
        self.assertEqual(cc("我明天要施肥", "harvest"), "unclear")
        self.assertEqual(cc("好，收成記下來", "harvest"), "yes")
        self.assertEqual(cc("施肥不用記", "fertilizer"), "no")
        self.assertEqual(cc("施肥不用記", "harvest"), "unclear")   # 提到別種事件、沒提本事件
        self.assertEqual(cc("對", "harvest"), "yes")
        self.assertEqual(cc("不用", "harvest"), "no")
        self.assertEqual(cc("等一下再記", "harvest"), "unclear")   # 未來／意向句
        self.assertEqual(cc("我想要施肥", "fertilizer"), "unclear")
        self.assertEqual(cc("確認要記錄空心菜", "harvest"), "yes")  # 「要記」仍是明確登記措辭

    def test_handler_passes_pending_type(self):
        # 2026-11-02 審查修正 D（A1）：handlers 攔截處必須把 pending["type"] 傳給 classify_confirmation
        import inspect
        import tg.handlers as Hd
        src = inspect.getsource(Hd.handle_message)
        self.assertIn('classify_confirmation(user_input_text, pending["type"])', src)

    def test_handler_does_not_return_on_unclear(self):
        import inspect
        import tg.handlers as Hd
        src = inspect.getsource(Hd.handle_message)
        seg = src[src.index('verdict = classify_confirmation'):src.index('# 對話重置指令')]
        # yes／no 各一個 return 分支之後，unclear 落下去不得再有 return
        tail = seg[seg.rfind("\n", 0, seg.index("verdict == unclear")):]  # 從該行行首起
        code_lines = [ln for ln in tail.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        self.assertFalse(any("return" in ln for ln in code_lines), code_lines)


# ---------------------------------------------------------------- A2
class _FakeResp:
    def __init__(self, data):
        self.ok, self.status_code, self._d, self.text = True, 200, data, ""

    def json(self):
        return self._d


class TestA2SensorFreshness(unittest.TestCase):
    def _run_scraper(self, hours_old):
        import scrapers.agri as A
        t_meas = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)
        point = {"time": t_meas.strftime("%Y-%m-%dT%H:%M:%SZ"), "air_temperature": 25.0,
                 "air_humidity": 80.0, "soil_temperature": 24.0, "soil_moisture": 28.0,
                 "soil_conductivity": 0.5}
        saved = []
        with mock.patch.object(A.requests, "get", return_value=_FakeResp({"data": [point]})), \
             mock.patch.object(A, "save_to_history", side_effect=lambda d: saved.append(d)), \
             mock.patch.object(A, "merge_temp_log", return_value=0), \
             mock.patch.object(A, "report_scraper_result"), \
             mock.patch.object(A, "_get_param_list", return_value=["air_temperature"]):
            out = json.loads(A.get_agriweather_data())
        return out, saved, t_meas

    def test_twelve_hours_old_is_stale(self):
        out, saved, t_meas = self._run_scraper(12)
        self.assertTrue(out["stale"])
        self.assertAlmostEqual(out["stale_hours"], 12.0, delta=0.2)
        expect = t_meas.astimezone(TZ_TAIPEI).isoformat(timespec="minutes")
        self.assertEqual(out["measured_at"], expect)
        self.assertEqual(saved[0]["measured_at"], expect)  # 歷史拿到的是量測時間

    def test_fresh_point_not_stale(self):
        out, _, _ = self._run_scraper(0.2)
        self.assertFalse(out["stale"])
        self.assertIsNotNone(out["measured_at"])

    def test_history_stores_measured_time(self):
        import storage.history as Hs
        tmp = tempfile.mkdtemp()
        old = Hs.DB_FILE
        Hs.DB_FILE = os.path.join(tmp, "h.db")
        try:
            c = sqlite3.connect(Hs.DB_FILE)
            c.execute("CREATE TABLE sensor_history (timestamp TEXT PRIMARY KEY, air_temperature TEXT, "
                      "air_humidity TEXT, soil_temperature TEXT, soil_humidity TEXT, soil_ec TEXT)")
            c.commit(); c.close()
            meas = (datetime.datetime.now(TZ_TAIPEI) - datetime.timedelta(hours=12)).replace(second=0, microsecond=0)
            Hs.save_to_history({"measured_at": meas.isoformat(timespec="minutes"), "air_temperature": "25.0 ℃"})
            Hs.save_to_history({"measured_at": meas.isoformat(timespec="minutes"), "air_temperature": "25.0 ℃"})
            rows = sqlite3.connect(Hs.DB_FILE).execute("SELECT timestamp FROM sensor_history").fetchall()
            self.assertEqual(rows, [(meas.strftime("%Y-%m-%d %H:%M:%S"),)])  # 量測時間、且同一筆不重複
        finally:
            Hs.DB_FILE = old

    def test_prompt_notice_and_sentinel_skip(self):
        from agent.prompts import parse_sensor_block, sensor_stale_notice
        d = {"soil_humidity": "10 %", "stale": True, "stale_hours": 12.0, "measured_at": "2026-11-02T08:00+08:00"}
        notice = sensor_stale_notice(d)
        self.assertIn("感測器已 12 小時未更新", notice)
        self.assertIn("舊值", notice)
        disp, *_ = parse_sensor_block(json.dumps(d))
        self.assertTrue(disp.startswith("⚠️ 感測器已"))
        self.assertEqual(sensor_stale_notice({"stale": False}), "")
        import inspect
        import services.sentinel as Se
        src = inspect.getsource(Se.hourly_safety_check_loop)
        self.assertIn('"sensor_stale"', src)
        # 舊值時把土濕／EC 值清掉，三個門檻警報就不會觸發
        self.assertIn("soil_hum_val = None", src[src.index("sensor_stale = bool"):])


# ---------------------------------------------------------------- A3
class TestA3InflightAndCancel(unittest.TestCase):
    def test_stuck_thread_blocks_second_message(self):
        import tg.handlers as Hd
        from agent.session import is_inflight
        from tg.api import start_capture, stop_capture
        blocker = threading.Event()
        calls = {"session": 0}

        def fake_get_or_create(chat_id, readonly=False):
            calls["session"] += 1
            return object()

        def stuck_send(chat, parts):
            blocker.wait(10)
            return mock.Mock(text="late")

        async def scenario():
            msg = {"chat": {"id": 123}, "text": "你好"}
            tok = start_capture()
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(Hd.handle_message(msg), timeout=0.5)
            finally:
                stop_capture(tok)
            self.assertTrue(is_inflight(123))          # 執行緒還在跑，旗標仍在
            tok = start_capture()
            await Hd.handle_message(msg)                 # 第二則
            reply = stop_capture(tok)
            self.assertIn("還在處理中", reply)
            self.assertEqual(calls["session"], 1)        # 沒有第二次進 session
            blocker.set()
            for _ in range(50):
                if not is_inflight(123):
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(is_inflight(123))         # 執行緒結束才清旗標

        with mock.patch.object(Hd, "get_or_create_chat", fake_get_or_create), \
             mock.patch.object(Hd, "send_message_with_retry", stuck_send), \
             mock.patch.object(Hd, "build_kb_context", return_value=""), \
             mock.patch.object(Hd, "load_fertilizer_events", return_value=""), \
             mock.patch.object(Hd, "load_prediction_feedback", return_value=""), \
             mock.patch.object(Hd, "load_last_push_brief", return_value=""), \
             mock.patch.object(Hd, "build_state_summary", return_value=""):
            asyncio.run(scenario())

    def test_cancelled_request_blocks_write_tools(self):
        import agent.tools as T
        from agent.session_guard import _cancel_ctx, CANCELLED_WRITE_NOTICE
        with _TempState() as ts:
            ts.S.update_state(lambda st: st["crops"].update({"萵苣 (Lettuce)": {"accumulated_gdd": 5.0, "last_gdd_date": "", "active": True}}))
            before = ts.S.load_state()
            flag = threading.Event()
            tok = _cancel_ctx.set(flag)
            try:
                flag.set()
                self.assertEqual(T.tool_set_crop("萵苣"), CANCELLED_WRITE_NOTICE)
                self.assertEqual(T.tool_set_thresholds(20, 40, "seedling"), CANCELLED_WRITE_NOTICE)
                self.assertEqual(T.tool_record_fertilizer_event("x"), CANCELLED_WRITE_NOTICE)
                self.assertEqual(ts.S.load_state(), before)   # 什麼都沒寫
                flag.clear()
                self.assertIn("已設為", T.tool_set_crop("萵苣"))  # 未取消時照常
            finally:
                _cancel_ctx.reset(tok)

    def test_all_write_tools_guarded(self):
        import inspect
        import agent.tools as T
        write_tools = [t for t in T.AGENT_TOOLS if t not in T.READONLY_TOOLS]
        self.assertTrue(write_tools)
        for t in write_tools:
            self.assertIn("cancelled_write_guard()", inspect.getsource(t), t.__name__)

    def test_run_model_turn_sets_cancel_flag(self):
        from agent.session import run_model_turn, is_inflight
        from agent.session_guard import is_request_cancelled
        seen = {}
        blocker = threading.Event()

        def work():
            blocker.wait(10)
            seen["cancelled"] = is_request_cancelled()
            return "done"

        async def go():
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(run_model_turn(777, False, work), timeout=0.3)
            self.assertTrue(is_inflight(777))
            blocker.set()
            for _ in range(50):
                if "cancelled" in seen and not is_inflight(777):
                    break
                await asyncio.sleep(0.05)
        asyncio.run(go())
        self.assertTrue(seen.get("cancelled"))   # 執行緒內看得到取消標記
        self.assertFalse(is_inflight(777))


# ---------------------------------------------------------------- A4
class TestA4WriteFailure(unittest.TestCase):
    def test_fertilizer_write_failure_restores_event(self):
        import agent.pending as P
        import storage.events as E
        tmp = tempfile.mkdtemp()
        cid = 880001
        with mock.patch.object(E, "EVENTS_FILE", os.path.join(tmp, "events.json")), \
             mock.patch.object(E, "atomic_write_json", side_effect=OSError("disk full")):
            self.assertFalse(E.save_fertilizer_event("x"))
            P.clear_pending_event(cid)
            self.assertTrue(P._set_pending_event(cid, "fertilizer", "施了有機肥"))
            msg = P.commit_pending_event(cid)
        try:
            self.assertIn("寫入失敗", msg)
            self.assertNotIn("已記錄", msg)
            self.assertIsNotNone(P.PENDING_EVENTS.get(cid))       # 事件放回待確認
            self.assertEqual(P.PENDING_EVENTS[cid]["type"], "fertilizer")
        finally:
            P.clear_pending_event(cid)

    def test_fertilizer_success_path(self):
        import agent.pending as P
        import storage.events as E
        tmp = tempfile.mkdtemp()
        cid = 880002
        with mock.patch.object(E, "EVENTS_FILE", os.path.join(tmp, "events.json")):
            P.clear_pending_event(cid)
            P._set_pending_event(cid, "fertilizer", "施了有機肥")
            msg = P.commit_pending_event(cid)
        self.assertIn("已記錄施肥事件", msg)
        self.assertIsNone(P.PENDING_EVENTS.get(cid))

    def test_harvest_write_failure_restores_event(self):
        import agent.pending as P
        import storage.harvest as H
        cid = 880003
        with _TempState() as ts:
            H._ensure_harvest_table()
            with mock.patch.object(H.sqlite3, "connect", side_effect=sqlite3.OperationalError("locked")):
                P.clear_pending_event(cid)
                P._set_pending_event(cid, "harvest", "x", crop_hint="")
                msg = P.commit_pending_event(cid)
            try:
                self.assertIn("收成記錄寫入失敗", msg)
                self.assertIsNotNone(P.PENDING_EVENTS.get(cid))
            finally:
                P.clear_pending_event(cid)

    def test_sow_write_failure_restores_event(self):
        import agent.pending as P
        import storage.state as S
        cid = 880004
        with _TempState():
            with mock.patch.object(S, "atomic_write_json", side_effect=OSError("ro")):
                P.clear_pending_event(cid)
                P._set_pending_event(cid, "sow", "", crop_hint="空心菜")
                msg = P.commit_pending_event(cid)
            try:
                self.assertIn("播種起算記錄寫入失敗", msg)
                self.assertIsNotNone(P.PENDING_EVENTS.get(cid))
            finally:
                P.clear_pending_event(cid)


# ---------------------------------------------------------------- A5
class TestA5FirstHarvestAfterResow(unittest.TestCase):
    def test_first_in_new_cycle_has_no_gdd_since_prev(self):
        with _TempState() as ts:
            crop = "空心菜 (Water Spinach)"
            ts.H._ensure_harvest_table()
            c = sqlite3.connect(ts.H.DB_FILE)
            # 上一輪：700 GDD 時割過一次（10 天前，在 45 天門檻內）
            c.execute("INSERT INTO harvest_records (harvest_date, crop_name, is_first_of_cycle, accumulated_gdd_snapshot, note, cycle_start_date) VALUES (?,?,?,?,?,?)",
                      (_days_ago(10), crop, 1, 700.0, "", _days_ago(40)))
            c.commit(); c.close()
            # 新播種（新 cycle_start），累積到 100 首採
            ts.S.update_state(lambda st: st["crops"].update({crop: {"accumulated_gdd": 100.0, "last_gdd_date": "",
                                                                    "active": True, "cycle_start_date": _days_ago(5)}}))
            msg = ts.H.record_harvest("", crop_hint="空心菜")
            self.assertIn("第一次割收", msg)
            row = sqlite3.connect(ts.H.DB_FILE).execute(
                "SELECT is_first_of_cycle, gdd_since_prev, days_since_prev FROM harvest_records "
                "WHERE crop_name=? ORDER BY id DESC LIMIT 1", (crop,)).fetchone()
            self.assertEqual(row[0], 1)
            self.assertIsNone(row[1])    # 不是 −600
            self.assertIsNone(row[2])


# ---------------------------------------------------------------- A6
class TestA6SetCropNoDuplicate(unittest.TestCase):
    def test_existing_custom_key_is_reused(self):
        import agent.tools as T
        with _TempState() as ts:
            ts.S.update_state(lambda st: st["crops"].update({"龍鬚菜": {"accumulated_gdd": 120.0, "last_gdd_date": "", "active": True}}))
            out = T.tool_set_crop("龍鬚菜")
            st = ts.S.load_state()
            keys = [k for k in st["crops"] if "龍鬚菜" in k]
            self.assertEqual(keys, ["龍鬚菜"])                 # 只有一個帳
            self.assertEqual(st["crops"]["龍鬚菜"]["accumulated_gdd"], 120.0)
            self.assertEqual(st["crop_name"], "龍鬚菜")       # 焦點指向它
            self.assertIn("120.0", out)


# ---------------------------------------------------------------- A7
class TestA7ResumeNoBackfill(unittest.TestCase):
    def test_resume_sets_last_gdd_date_to_yesterday(self):
        from science.gdd_engine import _pending_days_for, _settle_crop
        with _TempState() as ts:
            crop = "空心菜 (Water Spinach)"
            ts.S.update_state(lambda st: st["crops"].update({crop: {"accumulated_gdd": 100.0, "last_gdd_date": _days_ago(10), "active": False}}))
            key, acc = ts.S.set_crop_tracking("空心菜", True)
            e = ts.S.load_state()["crops"][key]
            self.assertEqual(acc, 100.0)
            self.assertEqual(e["last_gdd_date"], _days_ago(1))
            self.assertEqual(e["resumed_at"], _today())
            yesterday = datetime.datetime.now(TZ_TAIPEI).date() - datetime.timedelta(days=1)
            days, skipped = _pending_days_for(e["last_gdd_date"], yesterday)
            self.assertEqual(days, [])                        # 沒有待補算日
            r = _settle_crop(crop, days, skipped, _days_ago(1), {})
            self.assertEqual(r["added"], 0.0)                 # 不會變 190

    def test_already_active_untouched(self):
        with _TempState() as ts:
            crop = "空心菜 (Water Spinach)"
            ts.S.update_state(lambda st: st["crops"].update({crop: {"accumulated_gdd": 100.0, "last_gdd_date": _days_ago(3), "active": True}}))
            ts.S.set_crop_tracking("空心菜", True)
            e = ts.S.load_state()["crops"][crop]
            self.assertEqual(e["last_gdd_date"], _days_ago(3))   # 在種中的不動
            self.assertNotIn("resumed_at", e)


# ---------------------------------------------------------------- A8
class TestA8PushNotDelivered(unittest.TestCase):
    def test_failed_send_records_nothing(self):
        import services.push as Pu
        long_report = "🌱【定時農務建議推送】\n" + "1. 🌡️ 現況研判：土壤偏乾。\n" * 20
        calls = {"record": 0, "pred": 0}

        async def fake_send(*a, **k):
            return False

        async def fake_pred(*a, **k):
            calls["pred"] += 1

        with mock.patch.object(Pu, "get_agriweather_data", return_value=json.dumps({"soil_humidity": "20 %"})), \
             mock.patch.object(Pu, "get_cwa_weather_forecast", return_value="晴"), \
             mock.patch.object(Pu, "load_history_summary", return_value=""), \
             mock.patch.object(Pu, "load_fertilizer_events", return_value=""), \
             mock.patch.object(Pu, "load_daily_summaries", return_value=""), \
             mock.patch.object(Pu, "load_prediction_feedback", return_value=""), \
             mock.patch.object(Pu, "build_state_summary", return_value=""), \
             mock.patch.object(Pu, "build_disease_report", return_value=""), \
             mock.patch.object(Pu, "get_photo_staleness_days", return_value=3), \
             mock.patch.object(Pu, "generate_report_resilient", return_value=mock.Mock(text=long_report)), \
             mock.patch.object(Pu, "generate_report_text", return_value=""), \
             mock.patch.object(Pu, "send_telegram_message", fake_send), \
             mock.patch.object(Pu, "record_push", side_effect=lambda *a, **k: calls.__setitem__("record", calls["record"] + 1)), \
             mock.patch.object(Pu, "_register_daily_prediction", fake_pred):
            asyncio.run(Pu.trigger_scheduled_push())
        self.assertEqual(calls["record"], 0)   # 沒 record_push
        self.assertEqual(calls["pred"], 0)     # 沒登記預測

    def test_successful_send_still_records(self):
        import services.push as Pu
        long_report = "🌱【定時農務建議推送】\n" + "1. 🌡️ 現況研判：土壤偏乾。\n" * 20
        calls = {"record": 0, "pred": 0}

        async def fake_send(*a, **k):
            return True

        async def fake_pred(*a, **k):
            calls["pred"] += 1

        with mock.patch.object(Pu, "get_agriweather_data", return_value=json.dumps({"soil_humidity": "20 %"})), \
             mock.patch.object(Pu, "get_cwa_weather_forecast", return_value="晴"), \
             mock.patch.object(Pu, "load_history_summary", return_value=""), \
             mock.patch.object(Pu, "load_fertilizer_events", return_value=""), \
             mock.patch.object(Pu, "load_daily_summaries", return_value=""), \
             mock.patch.object(Pu, "load_prediction_feedback", return_value=""), \
             mock.patch.object(Pu, "build_state_summary", return_value=""), \
             mock.patch.object(Pu, "build_disease_report", return_value=""), \
             mock.patch.object(Pu, "get_photo_staleness_days", return_value=3), \
             mock.patch.object(Pu, "generate_report_resilient", return_value=mock.Mock(text=long_report)), \
             mock.patch.object(Pu, "send_telegram_message", fake_send), \
             mock.patch.object(Pu, "record_push", side_effect=lambda *a, **k: calls.__setitem__("record", calls["record"] + 1)), \
             mock.patch.object(Pu, "_register_daily_prediction", fake_pred):
            asyncio.run(Pu.trigger_scheduled_push())
        self.assertEqual(calls["record"], 1)
        self.assertEqual(calls["pred"], 1)


# ---------------------------------------------------------------- 照片備援措辭
class TestPhotoFallbackWording(unittest.TestCase):
    def test_text_only_prompt_declares_no_photo(self):
        from tg.handlers import _text_only_prompt
        user_block = ("使用者上傳了圖片（已附上兩張：第一張是系統從相簿找出的歷史照片、第二張是這次上傳的），並附帶文字說明：葉子有點黃"
                      "\n（第一步先依準則 7 判斷圖片類型：作物／田地實拍照才做作物診斷；歷史照片只在這次也是作物照時才拿來對比。）"
                      "\n（若是作物照：使用者的文字說明顯示這是一張『收成照片』，請依準則 14 呼叫 tool_record_harvest_event 登記收成。）"
                      "\n【系統背景資訊】土壤濕度 25%")
        out = _text_only_prompt([object(), object(), user_block])
        self.assertTrue(out.startswith("【本輪無法檢視照片】"))
        self.assertIn("無法檢視照片", out)
        self.assertIn("照片未能分析", out)
        self.assertNotIn("已附", out)
        self.assertNotIn("上傳了圖片", out)
        self.assertNotIn("上傳了一張圖片", out)
        self.assertNotIn("歷史照片只在", out)
        self.assertNotIn("tool_record_harvest_event", out)
        self.assertIn("葉子有點黃", out)           # 使用者文字保留
        self.assertIn("土壤濕度 25%", out)
        # 單張的寫法也要改寫
        out2 = _text_only_prompt([object(), "使用者上傳了一張圖片，並附帶文字說明：（沒有文字說明）"])
        self.assertNotIn("上傳了一張圖片", out2)
        self.assertIn("無法檢視照片", out2)

    def test_text_only_prompt_without_photo_unchanged(self):
        from tg.handlers import _text_only_prompt
        out = _text_only_prompt(["使用者訊息：「今天要澆水嗎」"])
        self.assertNotIn("無法檢視照片", out)
        self.assertIn("今天要澆水嗎", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
