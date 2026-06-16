# 持久化層：state.json 交易、SQLite（感測史/溫度日誌/日彙總/割收記錄）、
# 施肥事件、預測日誌、照片相簿。所有 JSON 讀寫持 STATE_FILE_LOCK 並原子寫入。
