# ======================================================================
# Playwright 智慧等待工具 (Smart Waits)
# ======================================================================
from logging_setup import logger


def smart_wait(page, selector, timeout_ms=15000, fallback_ms=2000, desc=""):
    """
    優先等待目標元素出現（元素一現身立即放行，加速且抗網路抖動）；
    逾時則記錄警告並退回固定等待，維持與舊版 wait_for_timeout 相同的
    「永不拋錯、慢慢來總會好」語義——確保等待策略升級不改變失敗行為。
    回傳是否命中元素（False 代表走了 fallback，部署初期可藉此監控選擇器是否失準）。
    """
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        return True
    except Exception:
        logger.warning(f"⏳ [Smart Wait] 等待「{desc or selector}」逾時 ({timeout_ms}ms)，退回固定等待 {fallback_ms}ms。若此警告頻繁出現，代表選擇器可能已隨網站改版失準。")
        page.wait_for_timeout(fallback_ms)
        return False


def wait_for_intercept(page, check_fn, timeout_ms=10000, poll_ms=250, desc="API 攔截"):
    """
    輪詢等待「網路回應攔截結果」就緒。這類等待的對象是 response 而非 DOM 元素，
    不能用 wait_for_selector 取代（畫面可能先渲染、API 後到，用元素等待會提早放行而漏接數據）。
    """
    waited = 0
    while not check_fn() and waited < timeout_ms:
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
    if not check_fn():
        logger.warning(f"⏳ [Smart Wait] {desc} 在 {timeout_ms}ms 內未就緒。")
        return False
    return True
