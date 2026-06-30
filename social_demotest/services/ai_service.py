import json
import google.generativeai as genai
from ollama import Client
from fastapi import HTTPException
from config import GOOGLE_API_KEY, GOOGLE_EMBEDDING_MODEL, OLLAMA_HOST, OLLAMA_API_KEY, OLLAMA_CHAT_MODEL

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

ollama_client = Client(
    host=OLLAMA_HOST,
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else None
)

def get_embedding(text: str) -> list:
    try:
        if not GOOGLE_API_KEY:
            raise RuntimeError("缺少 GOOGLE_AI_STUDIO_API_KEY（或 GOOGLE_API_KEY）")
        # 使用 Google AI Studio 免費額度的 embedding 模型
        result = genai.embed_content(
            model=GOOGLE_EMBEDDING_MODEL,
            content=text,
            task_type="retrieval_document",
        )
        return result['embedding']
    except Exception as e:
        print(f"Embedding error: {e}")
        raise HTTPException(status_code=500, detail=f"Google Embedding 錯誤: {e}")

def generate_chat_completion(prompt: str, temperature: float = 0.5, json_output: bool = False) -> str:
    if not OLLAMA_API_KEY:
        raise RuntimeError("缺少 OLLAMA_API_KEY，無法呼叫 Ollama Cloud 聊天模型")
    payload = {
        "model": OLLAMA_CHAT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": temperature},
    }
    if json_output:
        payload["format"] = "json"
    response = ollama_client.chat(**payload)
    content = response["message"]["content"].strip()
    
    if json_output:
        import re
        content = re.sub(r'^```json\s*', '', content)
        content = re.sub(r'^```\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        content = content.strip()
        
    return content

def analyze_big_five(text: str, previous_data: dict, interaction_count: int, initial_interest: str = None) -> dict:
    prev_str = json.dumps(previous_data, ensure_ascii=False) if previous_data else "無"
    
    interest_prompt = ""
    if initial_interest and interaction_count == 0:
        interest_prompt = f"【重點】：使用者在註冊時填寫的興趣是「{initial_interest}」。請務必以此興趣作為切入點，給予共鳴並設計第一個情境題。"
        
    prompt = f"""
    你叫「阿月」，是溫暖、有觀察力、閱人無數的 AI 媒人。你像熱心朋友，不像客服；
    可以偶爾善意吐槽或鼓勵，但不能刻薄，也不能讓使用者感到被評分。
    你的任務是一步一步、在自然對話中推測出使用者的大五人格 (Big Five)。

    【目前已知的性格數值】
    {prev_str}
    請根據使用者這一次的回覆，微調這些數值與 summary。若有尚未確認的特質，請維持原本數值並註明。

    【對話守則】
    1. 每次回覆與提問請保持「非常簡短（1~2句話以內）」，語氣輕鬆自然，像朋友閒聊。
    2. 每次只丟出「一個具體的情境題」，不提供選項，不要讓使用者覺得在做問卷。
    3. 針對使用者的前置回覆，先給予簡短共鳴後再發問。
    {interest_prompt}
    4. 第 {interaction_count + 1} 輪。只要達到 5 輪 (含) 以上，必須強制停止發問！將 "is_complete" 設為 true，並做性格總結。

    請回傳嚴格的純 JSON：
    1. "reply": "你給使用者的回覆"
    2. "big_five": {{"O": 1~10, "C": 1~10, "E": 1~10, "A": 1~10, "N": 1~10, "summary": "性格簡述"}}
    3. "is_complete": true 或是 false

    使用者說：{text}
    """
    try:
        content = generate_chat_completion(prompt, temperature=0.5, json_output=True)
        return json.loads(content)
    except Exception as e:
        print(f"analyze_big_five error: {e}")
        return {"reply": f"系統錯誤：{str(e)}", "big_five": previous_data, "is_complete": False}

def match_candidates(user_doc, candidates):
    prompt_candidates = ""
    candidate_scores = {}
    for idx, (score, c) in enumerate(candidates):
        bf = c.get('big_five', {})
        dp = c.get('deep_profile', {})
        # 支援我們的豐富結構與 GitHub 的簡化結構
        if isinstance(dp.get('values'), list):
            dp_str = f"價值觀: {', '.join(dp.get('values', []))} / 人生目標: {', '.join(dp.get('life_goals', []))} / 理想未來: {dp.get('ideal_future', '無')}" if dp else "無"
        else:
            dp_str = f"價值觀: {dp.get('values', '無')} / 未來規劃: {dp.get('future_plans', '無')}" if dp else "無"
        ctx = c.get('current_context', '隨意')
        candidate_scores[c.get('user_id')] = score
        prompt_candidates += f"\n【候選人 {idx+1} (ID: {c.get('user_id')})】\n性格: {bf}\n深層規劃: {dp_str}\n當下情境: {ctx}\n"

    user_dp = user_doc.get('deep_profile', {})
    if isinstance(user_dp.get('values'), list):
        user_dp_str = f"價值觀: {', '.join(user_dp.get('values', []))} / 人生目標: {', '.join(user_dp.get('life_goals', []))} / 理想未來: {user_dp.get('ideal_future', '無')}" if user_dp else "無"
    else:
        user_dp_str = f"價值觀: {user_dp.get('values', '無')} / 未來規劃: {user_dp.get('future_plans', '無')}" if user_dp else "無"

    prompt = f"""
    你是一位專業的繁體中文媒人。
    請比對「User A」的個性、價值觀與當下情境，從候選人中，挑出「性格互補且各方面最契合」的 1 位。
    
    【User A (ID: {user_doc.get('user_id')})】
    性格: {user_doc.get('big_five')}
    深層規劃: {user_dp_str}
    當下情境: {user_doc.get('current_context', '隨意')}
    
    -------------------
    候選人列表：
    {prompt_candidates}
    -------------------
    
    請回傳純 JSON 格式：
    {{
        "matched_user_id": "選出的候選人 ID",
        "reason": "生動白話的配對原因(繁體中文)。【極度重要】：請務必在理由中明確提及雙方的『價值觀』或『未來規劃』是如何契合（如果有提供的話）！並且務必用第三人稱客觀描述（例如：「User A 與 候選人...」），絕對不要使用「你」或「我」。"
    }}
    """
    try:
        content = generate_chat_completion(prompt, temperature=0.5, json_output=True)
        result = json.loads(content)
        
        # 在理由後方加上 Embedding 相似度，方便除錯與驗證
        matched_id = result.get("matched_user_id")
        if matched_id in candidate_scores:
            score_percent = round(candidate_scores[matched_id] * 100, 2)
            result["reason"] += f"\n\n(開發者驗證 - Embedding 相似度: {score_percent}%)"
            
        return result
    except Exception as e:
        print(f"match_candidates error: {e}")
        if candidates: 
            fallback_id = candidates[0][1]["user_id"]
            fallback_score = round(candidates[0][0] * 100, 2)
            return {"matched_user_id": fallback_id, "reason": f"AI 解析錯誤，由系統選擇相似度最高者。\n\n(開發者驗證 - Embedding 相似度: {fallback_score}%)"}
        return {"matched_user_id": "none", "reason": "沒有適合的人選。"}

def generate_peer_first_message(initiator_doc, target_doc, reason: str) -> str:
    prompt = f"""
    你現在要扮演使用者 {initiator_doc['user_id']} 對 {target_doc['user_id']} 發送第一句搭訕訊息。
    你的性格: {initiator_doc.get('big_five')}
    你們配對的原因: {reason}
    對方的近期想做的事情: {target_doc.get('current_context', '隨意')}
    
    要求：用繁體中文，語氣自然、像社交軟體的開場白，不要像機器人。根據你的性格風格來打招呼，並提到你們的共通點或他的近況。直接回傳對話內容。
    """
    try:
        return generate_chat_completion(prompt, temperature=0.5, json_output=False)
    except Exception as e:
        print(f"generate_peer_first_message error: {e}")
        return "嗨！很高興認識你～"

def summarize_context(message: str, previous_context: str = None) -> str:
    """將使用者訊息與先前情境融合，產生 10-15 字的近期情境摘要，用於 current_context 欄位"""
    if previous_context:
        prompt = f"""
    你是一位社交配對系統的情境摘要器。你的任務是將「使用者先前的情境」與「使用者最新說的話」融合，產生一個更新後的近期情境摘要。

    【使用者先前的情境】：{previous_context}
    【使用者最新說的話】：{message}

    【規則】
    1. 請綜合先前情境與最新訊息，產生一個 10-15 字以內的情境摘要。不要只是照抄使用者的原話，而是提煉出「這個人最近的核心動態或興趣」。
    2. 如果新訊息與先前情境相關，請融合為一個更完整的描述（例如：先前「想出國旅行」＋ 新訊息「想去日本看櫻花」→「計畫去日本賞櫻」）。
    3. 如果新訊息與先前情境無關，以新訊息為主，但用更精煉的方式表達（例如：「我最近在家裡一直看Netflix」→「熱衷追劇中」）。
    4. 注意時態：如果使用者表示事情「已經發生」，請如實記錄（例如「剛去過福岡玩」），不要寫成「想去」。
    5. 請直接回傳簡短描述，不要加引號或任何額外說明。

    範例：
    先前情境「想找户外活動」＋ 新訊息「我想去爬山」→ 「計畫去爬山」
    先前情境「喜歡看電影」＋ 新訊息「最近在學吉他」→ 「最近在學吉他」
    新訊息「去荒島求生」→ 「計畫荒島求生探險」
    """
    else:
        prompt = f"""
    你是一位社交配對系統的情境摘要器。請將使用者的訊息，提煉成一個 10-15 字以內的近期情境摘要。

    【規則】
    1. 不要只是照抄使用者的原話，而是提煉出「這個人最近的核心動態或興趣」。
    2. 用更精煉、更生動的方式表達（例如：「我最近在家裡一直看Netflix」→「熱衷追劇中」；「我想去海邊」→「想去海邊放鬆」）。
    3. 注意時態：如果使用者表示事情「已經發生」，請如實記錄（例如「剛去過福岡玩」），不要寫成「想去」。
    4. 請直接回傳簡短描述，不要加引號或任何額外說明。

    使用者說：{message}

    範例：「計畫去日本賞櫻」、「熱衷追劇中」、「剛去過福岡玩」、「想學吉他」
    """
    try:
        result = generate_chat_completion(prompt, temperature=0.3, json_output=False)
        return result.strip()[:20]
    except Exception as e:
        print(f"summarize_context error: {e}")
        return message[:15]

def analyze_deep_profile(text: str, previous_data: dict, interaction_count: int, user_context: dict = None) -> dict:
    """深層價值觀分析：探索使用者的核心價值觀、人生目標與深層需求"""
    prev_str = json.dumps(previous_data, ensure_ascii=False) if previous_data else "無"
    
    context_prompt = ""
    if user_context:
        bf_summary = user_context.get("big_five", {}).get("summary", "無")
        ctx = user_context.get("current_context", "無")
        context_prompt = f"\n    【使用者現有資料】\n    基本性格簡述: {bf_summary}\n    近期情境/興趣: {ctx}\n"
    
    prompt = f"""
    你叫「阿月」，是溫暖、有觀察力、閱人無數的 AI 媒人。你像熱心朋友，不像客服；
    可以偶爾善意吐槽或鼓勵，但不能刻薄，也不能讓使用者感到被審問。
    使用者已經完成了基本性格分析。現在你的任務是進一步在自然對話中推測出使用者的「深層價值觀」— 包含核心信念、人生目標、對關係的期待等。
{context_prompt}
    【目前已知的深層價值觀】
    {prev_str}
    請根據使用者這一次的回覆，微調或擴充這些深層特質。

    【對話守則】
    1. 每次回覆與提問請保持「非常簡短（1~2句話以內）」，語氣輕鬆自然，像朋友閒聊，不要給人壓力。
    2. 每次只丟出「一個」具體問題。請試著從對方的「近期情境或興趣」去延伸，自然地引導出他的價值觀與人生方向，而不是直接生硬地問。
    3.【極度重要】若這是第一輪對話，請直接從【使用者現有資料】（尤其是近期情境/興趣）作為切入點來發問！絕對不要一開始就問「最重要的事情是什麼」或「未來有什麼規劃」這種太廣泛的問題，請從具體、輕鬆的生活話題延伸。
    4. 針對使用者的前置回覆，先給予簡短共鳴後再發問。
    5. 第 {interaction_count + 1} 輪。只要達到 5 輪 (含) 以上，必須強制停止發問！將 "is_complete" 設為 true，並做深層總結。
    6. 探索方向包含但不限於：生活態度、人際期待、壓力應對、理想未來、情感需求。

    請回傳嚴格的純 JSON：
    1. "reply": "你給使用者的回覆（溫暖、有洞察力的追問或總結）"
    2. "deep_profile": {{
         "values": ["核心價值觀1", "核心價值觀2", ...],
         "life_goals": ["人生目標1", ...],
         "relationship_needs": ["關係需求1", ...],
         "stress_coping": "壓力應對方式簡述",
         "ideal_future": "理想未來簡述",
         "summary": "深層價值觀總結（50字內）"
       }}
    3. "is_complete": true 或是 false

    使用者說：{text}
    """
    try:
        content = generate_chat_completion(prompt, temperature=0.6, json_output=True)
        return json.loads(content)
    except Exception as e:
        print(f"analyze_deep_profile error: {e}")
        return {"reply": f"系統錯誤：{str(e)}", "deep_profile": previous_data, "is_complete": False}
