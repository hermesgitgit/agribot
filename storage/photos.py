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
# 照片相簿 (Photo Album) — 滾動保留 10 張，供跨時生長對比診斷
# ======================================================================
import datetime
import glob
import os

from config import PHOTO_DIR, TZ_TAIPEI
from logging_setup import logger


def save_photo(image_bytes) -> str:
    """
    將使用者上傳的照片實體存檔至照片目錄。
    檔名使用時間戳記以利定序。滾動刪除至最多保留 10 張。
    Returns:
        str: 儲存的實體相片絕對路徑。
    """
    os.makedirs(PHOTO_DIR, exist_ok=True)

    # 微秒精度檔名：避免同一秒內連續上傳互相覆寫；仍可字典序＝時間序。
    timestamp = datetime.datetime.now(TZ_TAIPEI).strftime("%Y%m%d_%H%M%S_%f")
    new_filepath = os.path.join(PHOTO_DIR, f"{timestamp}.jpg")

    try:
        with open(new_filepath, "wb") as f:
            f.write(image_bytes)
        logger.info(f"💾 [Photo Manager] 照片已儲存至: {new_filepath}")

        # 滾動刪除，保留最近 10 張照片
        photos = sorted(glob.glob(os.path.join(PHOTO_DIR, "*.jpg")))
        if len(photos) > 10:
            excess = len(photos) - 10
            for i in range(excess):
                try:
                    os.remove(photos[i])
                    logger.info(f"🧹 [Photo Manager] 已刪除過期老相片: {photos[i]}")
                except Exception as del_err:
                    logger.warning(f"⚠️ [Photo Manager] 刪除過期相片失敗: {del_err}")

        return new_filepath
    except Exception as e:
        logger.error(f"❌ [Photo Manager] 儲存相片失敗: {e}")
        return ""


def _parse_photo_dt(basename):
    """從相片檔名解析時間；同時相容新（微秒）與舊（秒）兩種格式。失敗回 None。"""
    stem = basename.split(".")[0]
    for fmt in ("%Y%m%d_%H%M%S_%f", "%Y%m%d_%H%M%S"):
        try:
            return datetime.datetime.strptime(stem, fmt)
        except ValueError:
            continue
    return None


def get_past_photo(current_photo_path: str) -> str:
    """
    在照片目錄中檢索「至少 4 天前、至多 14 天前」的歷史照片。
    若無此時間範圍的照片，則返回最舊的一張相片(但不能是當下這張)。
    Returns:
        str: 歷史相片的實體路徑，若無其他相片則返回 None。
    """
    if not os.path.exists(PHOTO_DIR):
        return None

    photos = sorted(glob.glob(os.path.join(PHOTO_DIR, "*.jpg")))
    # 過濾掉當前這張照片
    other_photos = [p for p in photos if p != current_photo_path]
    if not other_photos:
        return None

    # 解析當前照片的時間
    current_basename = os.path.basename(current_photo_path)
    current_dt = _parse_photo_dt(current_basename)
    if current_dt is None:
        # 若當前檔名無法解析，直接返回最舊的一張
        return other_photos[0]

    best_match = None
    # 尋找「至少 4 天前、至多 14 天前」的照片
    for p in reversed(other_photos):
        p_dt = _parse_photo_dt(os.path.basename(p))
        if p_dt is None:
            continue
        diff_days = (current_dt - p_dt).total_seconds() / 86400.0
        if 4.0 <= diff_days <= 14.0:
            best_match = p
            break

    if best_match:
        logger.info(f"🎞️ [Photo Manager] 找到完美的對照組歷史照片（差距 {diff_days:.1f} 天）: {best_match}")
        return best_match
    else:
        # 若找不到符合區間的對照組，則返回最舊的那張
        logger.info(f"🎞️ [Photo Manager] 未找到 4-14 天內之對照組，改採用最舊老相片為對照: {other_photos[0]}")
        return other_photos[0]


def get_photo_staleness_days():
    """回傳最新作物照片距今的天數；相簿為空或無法判讀時回傳 None。"""
    try:
        photos = sorted(glob.glob(os.path.join(PHOTO_DIR, "*.jpg")))
        if not photos:
            return None
        dt = _parse_photo_dt(os.path.basename(photos[-1]))  # 相容新(微秒)/舊(秒)檔名
        if dt is None:
            return None
        dt = dt.replace(tzinfo=TZ_TAIPEI)
        return max(0, (datetime.datetime.now(TZ_TAIPEI) - dt).days)
    except Exception:
        return None
