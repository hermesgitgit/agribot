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
# 阿龜原生「灌溉/施肥建議」爬蟲 (Playwright) — 與 API 感測路徑完全隔離
# ======================================================================
# 感測數據走官方 API（scrapers/agri.py，快、穩）；但阿龜的「原生灌溉/施肥建議」
# 只存在於網頁介面、無 API 端點，故仍以 Playwright 登入網頁爬取。
#
# 隔離設計：自己的鎖、自己的 try/except、回退化字串而非拋例外——
#   絕不影響 API 感測路徑。僅在設定了 AGRI_USERNAME/AGRI_PASSWORD 時啟用。
#   因含登入與選擇器、較慢（約 1~2 分鐘）且較脆弱，只由定時推播呼叫一次
#   （每小時的安全哨兵與一般對話走快速 API、不爬建議）。
# 安全：登入例外一律經 redact()（AGRI_PASSWORD 在遮罩清單），不讓帳密落盤。
import re
import threading

from playwright.sync_api import sync_playwright

from config import AGRI_PASSWORD, AGRI_USERNAME, redact
from logging_setup import logger
from scrapers.waits import smart_wait

ADVICE_LOCK = threading.Lock()  # 防止多條迴圈同時開 Chromium


def _extract_advice(text: str) -> str:
    """從頁面全文擷取「土壤質地推估…」起的建議段落。"""
    m = re.search(r'(土壤質地推估.*?)Agri-IoT', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'(土壤質地推估.*)', text, re.DOTALL)
    return m.group(1).strip() if m else "無建議或獲取失敗"


def fetch_native_advice():
    """
    登入阿龜網頁爬取原生灌溉與施肥建議，回傳 (灌溉建議, 施肥建議)。
    未設定帳密、或任何失敗時回退化字串——完全隔離，絕不向外拋出例外。
    """
    if not AGRI_USERNAME or not AGRI_PASSWORD:
        msg = "（未設定阿龜登入帳密，略過原生建議）"
        return msg, msg

    irrigation = fertilization = "無建議或獲取失敗"
    try:
        with ADVICE_LOCK, sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'])
            context = browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = context.new_page()

            logger.info("📍 [Advice] 登入阿龜微氣候網頁...")
            page.goto("https://account.agriweather.com.tw/")
            page.wait_for_selector('#email')
            page.fill('#email', AGRI_USERNAME)
            page.fill('#password', AGRI_PASSWORD)
            page.click('button.btn-primary')
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_timeout(5000)

            # --- 灌溉建議 ---
            try:
                logger.info("🚀 [Advice] 前往灌溉建議小工具...")
                page.goto("https://iot.agriweather.com.tw/", wait_until="networkidle")
                smart_wait(page, 'a[href="/tools/irrigation"]', 10000, 2000, "灌溉工具連結")
                page.click('a[href="/tools/irrigation"]')
                smart_wait(page, 'input[aria-label="選擇專案"]', 8000, 3000, "灌溉工具頁面")
                txt = page.evaluate("() => document.body.innerText")
                if "選擇專案" in txt:
                    page.click('input[aria-label="選擇專案"]')
                    smart_wait(page, '.q-manual-focusable', 5000, 1000, "專案下拉")
                    page.click('.q-manual-focusable')
                    page.wait_for_timeout(800)
                    page.click('input[aria-label^="選擇裝置"]')
                    smart_wait(page, '.q-manual-focusable', 5000, 1000, "裝置下拉")
                    page.click('.q-manual-focusable')
                    page.wait_for_timeout(800)
                    page.click('button:has-text("灌溉建議")')
                    smart_wait(page, 'text=土壤質地推估', 15000, 4000, "灌溉結果")
                    txt = page.evaluate("() => document.body.innerText")
                irrigation = _extract_advice(txt)
                logger.info("✅ [Advice] 灌溉建議抓取成功")
            except Exception as e:
                logger.warning(f"⚠️ [Advice] 灌溉建議抓取失敗: {redact(e)}")
                irrigation = "無法獲取灌溉建議"

            # --- 施肥建議 ---
            try:
                logger.info("🚀 [Advice] 前往施肥建議小工具...")
                page.goto("https://iot.agriweather.com.tw/", wait_until="networkidle")
                smart_wait(page, 'a[href="/tools/fertilization"]', 10000, 2000, "施肥工具連結")
                page.click('a[href="/tools/fertilization"]')
                smart_wait(page, 'input[aria-label="選擇專案"]', 8000, 3000, "施肥工具頁面")
                txt = page.evaluate("() => document.body.innerText")
                if "選擇專案" in txt:
                    page.click('input[aria-label="選擇專案"]')
                    smart_wait(page, '.q-item:has-text("采田福地")', 5000, 1000, "專案選項")
                    page.click('.q-item:has-text("采田福地")')
                    page.wait_for_timeout(800)
                    page.click('input[aria-label^="選擇裝置"]')
                    smart_wait(page, '.q-item', 5000, 1000, "裝置選項")
                    page.click('.q-item')
                    page.wait_for_timeout(800)
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(500)
                    page.click('button.action-btn')
                    smart_wait(page, 'text=土壤質地推估', 15000, 4000, "施肥結果")
                    txt = page.evaluate("() => document.body.innerText")
                fertilization = _extract_advice(txt)
                logger.info("✅ [Advice] 施肥建議抓取成功")
            except Exception as e:
                logger.warning(f"⚠️ [Advice] 施肥建議抓取失敗: {redact(e)}")
                fertilization = "無法獲取施肥建議"

            browser.close()
    except Exception as e:
        logger.warning(f"⚠️ [Advice] Playwright 整體失敗（不影響感測數據）: {redact(e)}")

    return irrigation, fertilization
