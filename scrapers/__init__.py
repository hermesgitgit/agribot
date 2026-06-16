# 資料擷取層：阿龜微氣候 Playwright 爬蟲（agri.py）、CWA 預報爬蟲與觀測 API（cwa.py）。
# 每個爬蟲有專屬鎖防多開 Chromium，成敗一律向 watchdog 記帳。
# 誠實原則：爬取失敗回報「無資訊」而非假資料，絕不以假數值充當實測。
