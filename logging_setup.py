# ======================================================================
# 日誌系統 (Logging)
# ======================================================================
# 取代散落的 print()：同時輸出至 stdout（維持 docker logs 即時查看的習慣）
# 與 /app/data/logs/ 下的輪替檔案（隨 NAS volume 持久化，容器重建不遺失），
# 附台北時區時間戳與 INFO/WARNING/ERROR 分級。
# Command Guard 的拒絕記錄、預測驗證結果等審計線索自此落盤可回溯。
import datetime
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = "/app/data/logs"


def _setup_logger():
    lg = logging.getLogger("agribot")
    if lg.handlers:  # 防止模組被重複匯入時疊加 handler
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    tz_taipei = datetime.timezone(datetime.timedelta(hours=8))
    fmt.converter = lambda *args: datetime.datetime.now(tz_taipei).timetuple()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(LOG_DIR, "agribot.log"),
            maxBytes=5 * 1024 * 1024,  # 單檔 5MB
            backupCount=5,             # 保留 5 份輪替（共約 30MB 上限）
            encoding="utf-8"
        )
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except Exception as log_err:
        lg.warning(f"⚠️ 無法建立檔案日誌，僅輸出至 stdout: {log_err}")

    lg.propagate = False
    return lg


logger = _setup_logger()
