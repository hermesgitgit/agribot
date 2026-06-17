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

# 科學運算層：GDD 積溫、ET₀ 蒸散、採收節律、預測自校正。
# 設計原則（依賴反轉）：本層的「純計算核心」一律以參數收資料、不直接讀
# state/DB；需要 I/O 的編排函數明確標示，並只透過 storage 層存取持久化資料。
