#!/bin/bash
# 每搬一步跑一次：帶 dummy 環境變數 import 門面模組，驗證搬遷未破壞任何 import 鏈
cd "$(dirname "$0")"
GEMINI_API_KEY=test AGRI_API_KEY=test AGRI_SUID=test \
TELEGRAM_TOKEN=test TELEGRAM_CHAT_ID=123 CWA_API_KEY=test \
python3 -c "import agriweather_scraper as m; print('IMPORT OK —', len([n for n in dir(m) if not n.startswith('__')]), 'public names')" 2>&1 | grep -v -E "Warning|warnings.warn|^$|google-gemini|README|updates or bug|switch to|^See |All support"
