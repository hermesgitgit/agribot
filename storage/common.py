# ======================================================================
# 持久化共用基座：狀態檔鎖與原子寫入
# ======================================================================
# 本程式有四條 asyncio 迴圈（Telegram 監聽、定時推播、安全哨兵、看門狗）
# 都可能透過 asyncio.to_thread 同時讀寫 JSON 狀態檔與 SQLite。
# 所有 state.json / events.json / predictions.json 與 SQLite 的存取都必須
# 持有 STATE_FILE_LOCK，防止讀寫交錯造成資料覆蓋遺失。
# （RLock：update_state 內部重入 load/save 取鎖是安全的。）
import json
import os
import tempfile
import threading

STATE_FILE_LOCK = threading.RLock()


def atomic_write_json(filepath, obj):
    """
    原子寫入 JSON：先寫入同目錄下的暫存檔，再以 os.replace() 原子性置換目標檔。
    這保證任何時間點檔案內容只會是「舊的完整版本」或「新的完整版本」，
    即使程式在寫入途中崩潰或斷電，也不會留下半截毀損的 JSON
    （毀損會導致 load_state 靜默退回預設值，累計 GDD 等資料歸零）。
    """
    dirpath = os.path.dirname(filepath) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
