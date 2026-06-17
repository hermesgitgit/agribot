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
# GDD 積溫引擎 — 純計算核心 (Science: Growing Degree Days)
# ======================================================================
# 本模組只含純函數與作物常數資料庫：給溫度與作物參數、回傳積溫，
# 不讀 state、不碰 DB。逐日結算與多日回補的編排在 engine.py。

# GDD 公式採用「上下限修正式」：日均溫先被夾在 [t_base, t_upper] 區間內再計算。
# t_upper（生長上限溫度）的意義：超過此溫度後作物生理活動趨於停滯甚至受抑制，
# 不應再線性累加積溫。台北夏季午後常超過 33℃，若無上限會系統性高估 GDD。
# target_gdd 為粗估參考值，請依實際品種與在地經驗微調。
CROP_GDD_DATABASE = {
    "水稻 (Rice)": {"t_base": 10.0, "t_upper": 30.0, "target_gdd": 2000.0},
    "玉米 (Corn)": {"t_base": 10.0, "t_upper": 30.0, "target_gdd": 1200.0},
    "番茄 (Tomato)": {"t_base": 10.0, "t_upper": 30.0, "target_gdd": 1000.0},
    "萵苣 (Lettuce)": {"t_base": 4.0, "t_upper": 24.0, "target_gdd": 600.0},
    "草莓 (Strawberry)": {"t_base": 10.0, "t_upper": 26.0, "target_gdd": 800.0},
    "馬鈴薯 (Potato)": {"t_base": 7.0, "t_upper": 26.0, "target_gdd": 1100.0},
    "高麗菜 (Cabbage)": {"t_base": 4.0, "t_upper": 24.0, "target_gdd": 900.0},
    "小黃瓜 (Cucumber)": {"t_base": 12.0, "t_upper": 32.0, "target_gdd": 1000.0},
    # 空心菜：嗜熱速生葉菜，基溫較高（約 15℃，低於此幾乎停止生長），
    # 耐熱性強故上限取 35℃。播種至首次採收約 25~35 天，目標積溫粗估 300 ℃-day，
    # 之後可連續割收，達標訊息可視為「首次採收」提示。
    "空心菜 (Water Spinach)": {"t_base": 15.0, "t_upper": 35.0, "target_gdd": 300.0},
    # 秋葵：嗜熱作物，基溫約 13℃、耐熱上限 35℃；播種至首採約 50~55 天，
    # 之後連續採收，目標積溫粗估 800 ℃-day（達標視為「首次採收」提示）。
    "秋葵 (Okra)": {"t_base": 13.0, "t_upper": 35.0, "target_gdd": 800.0},
    # 龍鬚菜（佛手瓜嫩梢）：蔓性、可連續割採的嫩梢，類似空心菜的割收模式；
    # 基溫約 10℃、上限 30℃，目標積溫粗估 500 ℃-day（首次可割參考）。
    "龍鬚菜 (Chayote Shoot)": {"t_base": 10.0, "t_upper": 30.0, "target_gdd": 500.0},
    "預設作物 (Default)": {"t_base": 10.0, "t_upper": 30.0, "target_gdd": 1000.0}
}

DEFAULT_CROP_KEY = "預設作物 (Default)"

GDD_BACKFILL_MAX_DAYS = 14  # 多日回補上限（含昨天），防止超長停機後一次結算過量


def match_crop_key(crop_name: str):
    """
    對資料庫做模糊比對（首詞互含），回傳命中的標準作物鍵；
    比對不到時回傳原名（以自訂名稱用預設參數註冊）。
    供作物切換路徑（AI 工具 / /crop 指令）使用。
    """
    for k in CROP_GDD_DATABASE.keys():
        if crop_name.split()[0] in k or k.split()[0] in crop_name:
            return k
    return crop_name


def lookup_crop_info(crop_name: str):
    """
    取得作物的 GDD 參數：先精確比對、再模糊比對（首詞互含），
    皆未命中時退用預設作物。回傳 (標準作物名, 參數 dict)。
    """
    info = CROP_GDD_DATABASE.get(crop_name)
    if info:
        return crop_name, info
    for k, v in CROP_GDD_DATABASE.items():
        if crop_name.split()[0] in k or k.split()[0] in crop_name:
            return k, v
    return DEFAULT_CROP_KEY, CROP_GDD_DATABASE[DEFAULT_CROP_KEY]


def gdd_from_minmax(t_min: float, t_max: float, t_base: float, t_upper: float) -> float:
    """
    上下限修正式 GDD 公式：將溫度夾在 [t_base, t_upper] 區間內，
    低於基溫不累積、高於上限溫不再線性增加（防止台北夏季高溫高估積溫）。
    """
    t_max_adj = min(max(t_max, t_base), t_upper)
    t_min_adj = min(max(t_min, t_base), t_upper)
    gdd = ((t_max_adj + t_min_adj) / 2.0) - t_base
    return round(max(0.0, gdd), 2)
