# 註：base image 標籤的 Playwright 版本必須與下方 pip install 的 playwright 版本「完全一致」，
# 否則內建的瀏覽器二進位會與 Python 套件版本對不上。要升版時兩處一起改。
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# 先複製並安裝所需套件 (這樣 Docker 可以快取這一步驟)
# Playwright 本身的瀏覽器已經內建在這個官方 image 中，不用再次下載
RUN pip install google-genai requests playwright==1.60.0 pillow

# 將程式複製進容器：頂層模組 + 六個套件目錄
# （data/、*.bak、.env 由 .dockerignore 排除，不會被烤進映像檔）
COPY *.py /app/
COPY agent/ /app/agent/
COPY science/ /app/science/
COPY scrapers/ /app/scrapers/
COPY services/ /app/services/
COPY storage/ /app/storage/
COPY tg/ /app/tg/

# 執行腳本（過渡門面，與拆分前的單檔介面完全相同；日後可改為 main.py）
CMD ["python3", "-u", "agriweather_scraper.py"]
