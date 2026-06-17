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
# 作物需水 — 純計算核心 (Science: Crop Water Demand, ETc)
# ======================================================================
# 把「全大氣通用」的參考蒸散量 ET₀ 轉成「這個作物、這個生長階段」的實際需水 ETc：
#     ETc = ET₀ × Kc
# 其中作物係數 Kc 隨生長階段變化（幼苗低、滿冠高、成熟回落），而「現在是哪個階段」
# 由 GDD 進度（accumulated / target）推得——這正是 GDD 與 ET₀ 的橋樑。
#
# 誠實原則：Kc 為逐作物的近似值、ET₀ 本身已標數據成色，兩個近似相乘不確定性會疊加，
# 故 ETc 一律當「有依據的參考」而非精確量測，土壤濕度實測仍是最終裁判。
# 本模組為純函數：GDD 進度與 ET₀ 由呼叫端傳入，不讀 state/DB。

# FAO-56 四階段 Kc 曲線的簡化版：以「成熟進度百分比」(GDD累計/目標) 切四段。
# 數值為各作物常見範圍的代表值；嗜熱速生葉菜（空心菜）滿冠 Kc 偏高、葉菜類略低。
# 階段門檻 (frac_lo, frac_hi]：初期→發育→中期(滿冠)→後期(成熟/衰老)。
_STAGE_BOUNDS = (0.20, 0.55, 0.85)  # <20% 初期；20~55% 發育；55~85% 中期；>85% 後期

# 各作物四階段 Kc：(初期, 中期滿冠, 後期)。發育期在初期與中期間線性內插。
# 查不到的作物用 DEFAULT。
_CROP_KC = {
    "空心菜 (Water Spinach)": (0.50, 1.15, 1.00),
    "萵苣 (Lettuce)":         (0.45, 1.00, 0.95),
    "高麗菜 (Cabbage)":       (0.45, 1.05, 0.95),
    "小白菜":                 (0.45, 1.00, 0.90),
    "番茄 (Tomato)":          (0.50, 1.15, 0.80),
    "小黃瓜 (Cucumber)":      (0.50, 1.10, 0.80),
    "草莓 (Strawberry)":      (0.45, 1.00, 0.80),
    "馬鈴薯 (Potato)":        (0.45, 1.10, 0.75),
    "玉米 (Corn)":            (0.40, 1.15, 0.70),
    "水稻 (Rice)":            (1.05, 1.20, 0.90),  # 水稻特殊：全程偏高
}
_DEFAULT_KC = (0.45, 1.05, 0.85)


def growth_stage(frac: float) -> str:
    """由成熟進度比例 frac (accumulated_gdd / target_gdd) 判定生長階段中文標籤。"""
    lo, mid, hi = _STAGE_BOUNDS
    if frac < lo:
        return "初期（幼苗）"
    if frac < mid:
        return "發育期（旺盛生長）"
    if frac < hi:
        return "中期（滿冠）"
    return "後期（成熟/採收）"


def crop_kc(crop_name: str, frac: float) -> float:
    """
    依作物與成熟進度比例回傳作物係數 Kc。
    初期/中期/後期取固定值，發育期在初期↔中期間線性內插（避免階段邊界跳變）。
    """
    kc_ini, kc_mid, kc_end = _CROP_KC.get(crop_name, _DEFAULT_KC)
    lo, mid, hi = _STAGE_BOUNDS
    if frac < lo:
        return kc_ini
    if frac < mid:  # 發育期：kc_ini → kc_mid 線性爬升
        return round(kc_ini + (kc_mid - kc_ini) * (frac - lo) / (mid - lo), 2)
    if frac < hi:
        return kc_mid
    # 後期：kc_mid → kc_end 線性回落（frac 夾在 [hi, 1.2] 內，>1.2 視為已達 kc_end）
    span = min(max(frac, hi), 1.2)
    return round(kc_mid + (kc_end - kc_mid) * (span - hi) / (1.2 - hi), 2)


def crop_water_demand(crop_name: str, accumulated_gdd: float, target_gdd: float, et0_mm):
    """
    計算單一作物今日的作物需水 ETc = ET₀ × Kc。
    回傳 dict：stage / kc / etc / frac。et0_mm 為 None（無法取得 ET₀）時 etc 也為 None。
    """
    try:
        frac = float(accumulated_gdd) / float(target_gdd) if target_gdd else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        frac = 0.0
    frac = max(0.0, frac)
    kc = crop_kc(crop_name, frac)
    etc = None
    if et0_mm is not None:
        try:
            etc = round(float(et0_mm) * kc, 2)
        except (TypeError, ValueError):
            etc = None
    return {"stage": growth_stage(frac), "kc": kc, "etc": etc, "frac": round(frac, 3)}
