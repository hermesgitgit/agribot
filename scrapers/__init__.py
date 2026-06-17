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

# 資料擷取層：阿龜微氣候 Playwright 爬蟲（agri.py）、CWA 預報爬蟲與觀測 API（cwa.py）。
# 每個爬蟲有專屬鎖防多開 Chromium，成敗一律向 watchdog 記帳。
# 誠實原則：爬取失敗回報「無資訊」而非假資料，絕不以假數值充當實測。
