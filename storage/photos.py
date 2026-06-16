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

    timestamp = datetime.datetime.now(TZ_TAIPEI).strftime("%Y%m%d_%H%M%S")
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
    try:
        current_dt = datetime.datetime.strptime(current_basename.split(".")[0], "%Y%m%d_%H%M%S")
    except Exception:
        # 若當前檔名無法解析，直接返回最舊的一張
        return other_photos[0]

    best_match = None
    # 尋找「至少 4 天前、至多 14 天前」的照片
    for p in reversed(other_photos):
        base = os.path.basename(p)
        try:
            p_dt = datetime.datetime.strptime(base.split(".")[0], "%Y%m%d_%H%M%S")
            diff_days = (current_dt - p_dt).total_seconds() / 86400.0
            if 4.0 <= diff_days <= 14.0:
                best_match = p
                break
        except Exception:
            continue

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
        newest = os.path.basename(photos[-1]).replace(".jpg", "")
        dt = datetime.datetime.strptime(newest, "%Y%m%d_%H%M%S")
        dt = dt.replace(tzinfo=TZ_TAIPEI)
        return max(0, (datetime.datetime.now(TZ_TAIPEI) - dt).days)
    except Exception:
        return None
