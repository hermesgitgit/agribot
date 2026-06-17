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
# 割收節律歸納 — 純邏輯核心 (Science: Harvest Cadence)
# ======================================================================
# 針對空心菜這類「可連續割收」的作物：每次割收由使用者以 /harvest 指令登記，
# 系統記下該次割收的日期、與上次割收的間隔天數、以及這段期間累積的 GDD，
# 從中歸納出兩種週期——日曆週期（約幾天割一次）與積溫週期（每次約累積多少 ℃-day）。
#
# 設計要點：
# 1. 區分「建立期」（定植/切換作物後的首次割收）與「再生期」（其後每次割收），
#    兩者生物學意義不同，分別歸納。作物切換（tool_set_crop / /crop）會重設基準。
# 2. 誠實標註樣本數：再生週期樣本 < HARVEST_MIN_SAMPLES 時明示為「初步觀察」而非穩定週期。
# 3. 同時呈現「近期」與「全期」週期，緩解空心菜的季節性生長速度漂移。
# 4. 每筆記錄連同當下作物名稱與累計 GDD 快照存檔，使「期間 GDD」可由相鄰兩筆相減得出。
#
# 本模組為純函數：割收間隔資料由呼叫端傳入（DB I/O 在 storage/harvest.py）。
import datetime

HARVEST_MIN_SAMPLES = 4  # 再生週期達此樣本數才視為「可信」而非「初步觀察」
HARVEST_RECENT_WINDOW = 3  # 「近期週期」採計最近幾次再生間隔


def mean(xs):
    return round(sum(xs) / len(xs), 1) if xs else None


def cadence_brief(days, gdds) -> str:
    """一句話的節律摘要（附在割收確認訊息後）。輸入為再生間隔天數與期間 GDD 列表。"""
    if not days:
        return "（尚無完整的再生週期，再記錄一次割收即可開始歸納。）"
    recent = days[-HARVEST_RECENT_WINDOW:]
    if len(days) < HARVEST_MIN_SAMPLES:
        return (f"📊 初步觀察（僅 {len(days)} 個週期，尚不穩定）："
                f"目前平均每 {mean(days)} 天割一次。")
    msg = f"📊 割收節律（{len(days)} 個再生週期）：近期約每 {mean(recent)} 天、全期平均每 {mean(days)} 天割一次"
    if gdds:
        msg += f"；每輪約累積 {mean(gdds)} ℃-day。"
    else:
        msg += "。"
    return msg


def cadence_summary_lines(crop_name, total, first, last, days, gdds) -> str:
    """
    完整的割收節律摘要文字。total 為割收總次數；first/last 為最早/最近割收日
    （DB 查詢的單欄 tuple 或 None）；days/gdds 為再生間隔與期間 GDD 列表。
    """
    if total == 0:
        return f"【割收節律】{crop_name}：尚無割收記錄。每次割收後以 /harvest 登記，即可逐步歸納出採收週期。"

    lines = [f"【割收節律歸納 — {crop_name}】",
             f"- 累計割收次數：{total} 次（最早 {first[0] if first else '—'}，最近 {last[0] if last else '—'}）"]
    if not days:
        lines.append("- 尚無完整再生週期（僅有建立期首割），再記錄一次即可開始歸納。")
        return "\n".join(lines)
    recent = days[-HARVEST_RECENT_WINDOW:]
    confidence = "可信" if len(days) >= HARVEST_MIN_SAMPLES else f"初步觀察（樣本僅 {len(days)}，未達 {HARVEST_MIN_SAMPLES}）"
    lines.append(f"- 再生週期樣本數：{len(days)}（{confidence}）")
    lines.append(f"- 日曆週期：近期約每 {mean(recent)} 天、全期平均每 {mean(days)} 天割一次（範圍 {min(days)}~{max(days)} 天）")
    if gdds:
        lines.append(f"- 積溫週期：每輪平均累積約 {mean(gdds)} ℃-day（範圍 {round(min(gdds),1)}~{round(max(gdds),1)}）")
    # 預估下次可割日
    if last and recent:
        try:
            last_date = datetime.datetime.strptime(last[0], "%Y-%m-%d")
            next_date = last_date + datetime.timedelta(days=round(mean(recent)))
            lines.append(f"- 依近期節律推估：下次約可在 {next_date.strftime('%Y-%m-%d')} 前後割收。")
        except Exception:
            pass
    return "\n".join(lines)
