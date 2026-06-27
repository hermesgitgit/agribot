"""Gemini 壅塞時的報告備援：用 NVIDIA 的強模型以「純文字」產出報告。

只在 Gemini（含自動重試）整個生不出報告時，由呼叫端當最後一道安全網叫用。
報告所需資料已全部寫在 prompt 裡（感測、天氣、歷史、病害、知識庫節錄），
所以純文字模型不必呼叫工具也能寫——這正是備援可行、且不降級內容的原因。

OpenAI 相容端點，原生 urllib，不增加任何套件依賴。金鑰讀 config.NVIDIA_API_KEY。
"""

import json
import urllib.request
import urllib.error

from config import NVIDIA_API_KEY, redact
from logging_setup import logger

BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"  # 非中國的強推理模型；報告品質導向


def is_configured() -> bool:
    return bool(NVIDIA_API_KEY)


def generate_report_text(prompt, timeout=120) -> str:
    """用 NVIDIA 模型把 prompt 生成報告純文字。成功回字串；未設金鑰或任何失敗回 ''。

    刻意回 '' 而非拋例外，讓呼叫端用「真值判斷」決定要不要再退到放棄訊息。
    """
    if not NVIDIA_API_KEY:
        return ""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2048,
    }).encode("utf-8")
    req = urllib.request.Request(BASE_URL, data=body, headers={
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        # 推理型模型（gpt-oss）最終答案在 content；保險再看 reasoning_content
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()
    except Exception as e:
        logger.warning(f"⚠️ [Fallback] NVIDIA 備援報告生成失敗: {redact(e)}")
        return ""
