# ======================================================================
# 視覺生長評估 — 純邏輯核心 (Science: Visual Phenology Cross-check)
# ======================================================================
# 把使用者上傳照片的「視覺觀察階段」與 GDD 熱量時間推估的階段做交叉檢核：
# 照片是「現場真相」、GDD 是「模型預測」，兩者背離即是警訊（逆境/天氣/定植）
# 與校正來源（GDD 參數是否適配此微氣候）。
# 本模組為純函數：不讀 state/DB、不碰影像；視覺評估由 AI（多模態）產出後傳入。
#
# 誠實原則：視覺評估是粗分級的定性判斷（受光線/角度影響），非精確量測；
# 照片零星上傳、非規律時間序列。交叉檢核只給「方向性」訊號，不宣稱精準。

# 與 science/water.growth_stage 對齊的五個有序階段（含「未知」）。
# 序號用來比較視覺 vs GDD 的相對前後，不代表精確進度。
STAGE_ORDER = ["初期（幼苗）", "發育期（旺盛生長）", "中期（滿冠）", "後期（成熟/採收）"]
_STAGE_INDEX = {s: i for i, s in enumerate(STAGE_ORDER)}

# 視覺評估的合法分級（生長勢、冠層覆蓋度），供登記時驗證。
VIGOR_SCALE = (1, 5)      # 1=極弱/萎黃 … 5=極旺盛/濃綠
COVERAGE_SCALE = (1, 5)   # 1=零星裸露 … 5=完全覆蓋


def normalize_stage(label: str):
    """
    把 AI 給的階段文字寬鬆對映到 STAGE_ORDER 之一，回傳 (標準階段, 序號)；
    無法判讀時回 (None, None)。接受同義詞（幼苗/苗期、開花/結球→後期 等）。
    """
    if not label:
        return None, None
    t = str(label)
    # 先試精確/子字串命中標準標籤
    for s in STAGE_ORDER:
        key = s.split("（")[0]
        if key in t or s in t:
            return s, _STAGE_INDEX[s]
    # 同義詞對映
    syn = [
        (["幼苗", "苗期", "剛種", "剛播", "定植", "出芽", "本葉"], "初期（幼苗）"),
        (["旺盛", "發育", "營養生長", "抽長", "展葉", "成長期"], "發育期（旺盛生長）"),
        (["滿冠", "封行", "中期", "茂盛", "成株"], "中期（滿冠）"),
        (["成熟", "採收", "可採", "開花", "結球", "結果", "抽苔", "老化", "衰老"], "後期（成熟/採收）"),
    ]
    for keys, std in syn:
        if any(k in t for k in keys):
            return std, _STAGE_INDEX[std]
    return None, None


def crosscheck_stage(visual_stage_label: str, gdd_stage_label: str) -> dict:
    """
    交叉檢核「視覺觀察階段」與「GDD 推估階段」。
    回傳 {visual, gdd, delta, verdict, note}：
      delta = 視覺序號 − GDD 序號（正=視覺超前模型、負=落後、0=一致）；
      verdict ∈ {aligned, ahead, behind, unknown}。
    無法判讀任一邊時 verdict=unknown，不妄下結論（誠實原則）。
    """
    v_std, v_idx = normalize_stage(visual_stage_label)
    g_std, g_idx = normalize_stage(gdd_stage_label)
    if v_idx is None or g_idx is None:
        return {"visual": v_std, "gdd": g_std, "delta": None,
                "verdict": "unknown", "note": "視覺或 GDD 階段無法判讀，本次不做交叉檢核。"}
    delta = v_idx - g_idx
    if delta == 0:
        verdict, note = "aligned", "視覺現況與 GDD 積溫推估一致，模型可信。"
    elif delta > 0:
        verdict, note = "ahead", ("照片顯示作物比積溫推估更成熟——可能此微氣候生長較快，"
                                   "或目標積溫設定偏高，可考慮下修該作物的成熟目標。")
    else:
        verdict, note = "behind", ("照片顯示作物落後於積溫推估——常見原因：缺水/養分逆境、"
                                   "近期低溫、定植不良或病蟲害。建議優先排查現場狀況。")
    return {"visual": v_std, "gdd": g_std, "delta": delta, "verdict": verdict, "note": note}


def validate_score(value, scale):
    """把 AI 給的 1~5 分級夾到合法範圍；非數值回 None。"""
    lo, hi = scale
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return None
