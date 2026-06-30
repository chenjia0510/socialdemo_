import json
import time
import os
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pymongo import ReturnDocument
from models import ChatRequest, DirectChatRequest, MediatorPrivateRequest, MediatorProbeRequest, ResetRequest
from database import profiles_coll, messages_coll, matches_coll
from services.ai_service import analyze_big_five, analyze_deep_profile, get_embedding, generate_chat_completion
from services.chat_service import generate_room_id, save_message
from services.memory_service import observe_user_memory

router = APIRouter(prefix="/api", tags=["Chat"])
MATCH_READINESS_THRESHOLD = 75
FEEDBACK_COOLDOWN_SECONDS = 120
PROBE_PENDING_TTL = 72 * 3600

MEDIATOR_PERSONA = """
你叫「阿月」，是一位閱人無數、溫暖又有觀察力的媒人。
你不是客服，說話自然、有分寸，偶爾善意吐槽；不施壓、不替人補故事。
請一律使用自然繁體中文，預設只回一句，必要時最多兩句，不用標題或條列。
"""

MEDIATOR_TONES = {
    "friend": "像熟朋友，口語、直率，偶爾說『欸』或『我跟你說』，但別每句都用。",
    "gentle": "像溫柔姐姐，穩定細膩、讓人安心，不說教。",
    "enthusiastic": "像熱心媒婆，活潑有戲但不浮誇，也不替任何人施壓。"
}

RELATIONSHIP_EVENT_TYPES = {
    "feedback_request", "feedback_consent_request", "probe_result",
    "gentle_closure", "mutual_interest"
}

def mediator_style(user_id: str) -> str:
    doc = profiles_coll.find_one({"user_id": user_id}, {"mediator_tone": 1}) or {}
    tone = doc.get("mediator_tone", "friend")
    return MEDIATOR_TONES.get(tone, MEDIATOR_TONES["friend"])

def relationship_unread_field(match_doc: dict, user_id: str) -> str:
    role = "from" if match_doc.get("from_user") == user_id else "to"
    return f"private_unread.{role}"

def participant_role(match_doc: dict, user_id: str) -> str:
    return "from" if match_doc.get("from_user") == user_id else "to"

def participant_probe_state(match_doc: dict, user_id: str) -> dict:
    return (((match_doc.get("mediator_state") or {}).get("participants") or {}).get(participant_role(match_doc, user_id)) or {})

def participant_probe_field(match_doc: dict, user_id: str) -> str:
    return f"mediator_state.participants.{participant_role(match_doc, user_id)}"

def probe_policy(user_id: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {"probe_mode": 1}) or {}
    mode = doc.get("probe_mode", "balanced")
    if mode == "manual":
        return mode, 10**9, 10**9, 86400
    if mode == "active":
        return mode, 6, 600, 21600
    if os.getenv("MEDIATOR_DEMO_FAST_PROBE", "0") == "1":
        return mode, 6, 120, 300
    return mode, 8, 1800, 86400

def queue_mediator_event(user_id: str, message: str, event_type: str, **extra):
    event = {
        "type": event_type,
        "message": message,
        "created_at": time.time(),
        **extra
    }
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$push": {"mediator_inbox": event}},
        upsert=True
    )

def trigger_proactive_match(user_id: str, source: str = "automatic", force_new: bool = False):
    from routers.match import create_proactive_match_proposal
    create_proactive_match_proposal(user_id, source=source, force_new=force_new)

def classify_feedback(message: str) -> str:
    positive_words = ("喜歡", "不錯", "有感覺", "聊得來", "可以", "想再聊", "有興趣", "很棒", "舒服")
    negative_words = ("不喜歡", "沒感覺", "不適合", "不太適合", "尷尬", "不習慣", "不想", "還好", "算了")
    if any(word in message for word in negative_words):
        return "negative"
    if any(word in message for word in positive_words):
        return "positive"
    try:
        prompt = f"""
請判斷這句對約會／聊天對象的回饋情緒：「{message}」
只回傳 JSON：{{"sentiment":"positive|negative|neutral"}}
"""
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        sentiment = result.get("sentiment", "neutral")
        return sentiment if sentiment in {"positive", "negative", "neutral"} else "neutral"
    except Exception:
        return "neutral"

def classify_proposal_intent(message: str):
    positive_words = ("有興趣", "想認識", "可以", "好啊", "願意", "幫我問", "接受")
    negative_words = ("先不要", "沒興趣", "不適合", "婉拒", "不要", "算了")
    if any(word in message for word in negative_words):
        return False
    if any(word in message for word in positive_words):
        return True
    return None

def handle_private_feedback(user_id: str, user_doc: dict, message: str):
    match_id = user_doc.get("pending_feedback_match_id")
    if not match_id:
        return None
    try:
        from bson.objectid import ObjectId
        match_doc = matches_coll.find_one({"_id": ObjectId(match_id)})
    except Exception:
        match_doc = None
    if not match_doc:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}}
        )
        return None

    other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
    sentiment = classify_feedback(message)
    share_consent = any(phrase in message for phrase in (
        "可以透露", "可以跟他說", "可以告訴他", "幫我轉告",
        "願意分享", "請婉轉收尾", "[同意分享]"
    ))
    feedback_entry = {
        "sentiment": sentiment,
        "share_consent": share_consent,
        "updated_at": time.time()
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            f"private_feedback.{user_id}": feedback_entry,
            f"private_feedback_text.{user_id}": message,
        }}
    )
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}}
    )

    refreshed = matches_coll.find_one({"_id": match_doc["_id"]})
    feedback = refreshed.get("private_feedback", {})
    other_feedback = feedback.get(other_id, {})
    if isinstance(other_feedback, str):
        other_feedback = {"sentiment": other_feedback, "share_consent": False}
    other_sentiment = other_feedback.get("sentiment")
    other_consent = bool(other_feedback.get("share_consent"))
    probe_requesters = set(refreshed.get("probe_requested_by", []))

    if other_id in probe_requesters and share_consent:
        if sentiment == "positive":
            queue_mediator_event(
                other_id,
                f"偷偷跟你說，我替你問過了：{user_id} 對你是有好感的。"
                "這是對方同意我透露的大方向，不是我偷聽來的；可以放心自然往前一步。",
                "probe_result",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        elif sentiment == "negative":
            queue_mediator_event(
                other_id,
                "我替你問過了，你們現在的節奏沒有完全對上。不是誰不好，"
                "只是火花不能硬擦；先把體面留好，我會幫你顧好收尾。",
                "gentle_closure",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$pull": {"probe_requested_by": other_id}}
        )

    if sentiment == "positive" and share_consent and other_sentiment == "positive" and other_consent:
        for recipient, crush in ((user_id, other_id), (other_id, user_id)):
            queue_mediator_event(
                recipient,
                f"偷偷跟你說，{crush} 對你的印象也超級好。你可以放心約週末了——"
                "都到這一步還只聊天氣，我這個媒人會替你們著急啦。",
                "mutual_interest",
                match_id=str(match_doc["_id"]),
                other_id=crush
            )
        return (
            f"我就知道你會注意到 {other_id} 的好。更巧的是，對方也對你很有好感。"
            "放心往前一步吧，我在後面幫你顧氣氛。"
        )

    if sentiment == "negative":
        if share_consent and other_sentiment == "positive" and other_consent:
            queue_mediator_event(
                other_id,
                "我幫你探過口風了，你們目前的節奏有一點不一樣。不是你不好，"
                "只是火花這種東西不能硬擦；先把體面留好，下一位我再替你仔細看。",
                "gentle_closure",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        if share_consent:
            return (
                "收到，我會替你把節奏放慢、把台階留好，不會轉述你的原話。"
                "感覺不對不用勉強演偶像劇，這也是媒人的正職。"
            )
        return (
            "收到，我懂。感覺不對就不用勉強演偶像劇，我會替你把話說得漂亮，"
            "而且你沒有同意分享，所以這段話只留在我們之間。"
        )

    if sentiment == "positive":
        if share_consent:
            return (
                f"喔～我有聽到那個「有點可以」的語氣了。你也同意我稍微透露，"
                f"那我會私下探探 {other_id} 的口風，但不會搬運你的原話。"
            )
        return (
            "好感我收到了，但你沒有同意我轉告，所以我先替你保密。"
            "想讓我稍微推一把時，再跟我說「可以透露」。"
        )
    return "我懂，你還在觀察。那就先自然聊，不用急著替關係下標題；好感又不是期中考，不必現在交卷。"

def mark_post_chat_activity(match_doc: dict, room_id: str):
    if not match_doc:
        return 0
    count = messages_coll.count_documents({"room_id": room_id})
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"shared_message_count": count, "last_chat_at": time.time()}}
    )
    return count

def summarize_relationship(match_id, room_id: str):
    match_doc = matches_coll.find_one({"_id": match_id})
    if not match_doc:
        return
    count = messages_coll.count_documents({"room_id": room_id})
    memory = match_doc.get("relationship_memory", {}) or {}
    if count < 6 or count - int(memory.get("last_summarized_count", 0)) < 4:
        return
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1}
    ).sort("timestamp", -1).limit(20))[::-1]
    transcript = "\n".join(f"{m['sender_id']}: {m['content']}" for m in history)
    prompt = f"""
只分析這段雙方都看得到的配對聊天室，不推測未說出的事，也不要保存逐字原話。
請回傳 JSON：{{"shared_summary":"一到兩句摘要","interaction_tone":"氣氛",
"common_topics":["共同話題"],"conversation_hooks":["可自然延伸的話題"]}}
聊天室：
{transcript}
"""
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0.2, json_output=True))
        data["last_summarized_count"] = count
        data["updated_at"] = time.time()
        matches_coll.update_one({"_id": match_id}, {"$set": {"relationship_memory": data}})
    except Exception as e:
        print(f"Relationship summary error: {e}")

def queue_due_feedback(user_id: str):
    mode, min_messages, idle_seconds, cooldown_seconds = probe_policy(user_id)
    if mode == "manual":
        return
    now = time.time()
    candidates = list(matches_coll.find({"status": "accepted", "$or": [{"from_user": user_id}, {"to_user": user_id}]}))
    for match_doc in candidates:
        count = int(match_doc.get("shared_message_count", 0))
        if count < min_messages or float(match_doc.get("last_chat_at", now)) > now - idle_seconds:
            continue
        state = participant_probe_state(match_doc, user_id)
        status = state.get("status", "idle")
        if status in {"queued", "awaiting_sentiment", "awaiting_consent"}:
            if float(state.get("asked_at", now)) < now - PROBE_PENDING_TTL:
                matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
                    participant_probe_field(match_doc, user_id) + ".status": "expired"}})
            continue
        last_count = int(state.get("message_count_snapshot", 0))
        if state.get("completed_at") and (count - last_count < 6 or now < float(state.get("cooldown_until", 0))):
            continue
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        probe_state = {"status": "queued", "trigger": "auto", "requester_id": None,
                       "asked_at": now, "message_count_snapshot": count,
                       "cooldown_until": now + cooldown_seconds}
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
            participant_probe_field(match_doc, user_id): probe_state}})
        queue_mediator_event(user_id, f"剛剛跟 {other_id} 聊起來感覺怎樣？", "feedback_request",
            match_id=str(match_doc["_id"]), other_id=other_id, origin="auto")
        return

@router.post("/chat")
def chat_endpoint(req: ChatRequest):
    if req.state == "big_five":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_big_five = user_doc.get("temp_big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count", 0) if user_doc else 0
        
        result = analyze_big_five(req.message, prev_big_five, interaction_count, req.initial_interest)
        
        update_fields = {
            "temp_big_five": result.get("big_five", {}),
            "interaction_count": interaction_count + 1
        }
        
        if result.get("is_complete", False):
            update_fields["big_five"] = result.get("big_five", {})
            room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": room_id})
            if count == 0:
                save_message(room_id, "ai_assistant", "好，性格底牌我大概看懂了。別緊張，我不是要算命啦——最近有沒有特別想做的事或想去的地方？")

        profiles_coll.update_one(
            {"user_id": req.user_id}, 
            {"$set": update_fields}, 
            upsert=True
        )

        return {
            "status": "success", 
            "big_five": result.get("big_five"), 
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }
    elif req.state == "deep_profile":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_deep = user_doc.get("temp_deep_profile", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count_deep", 0) if user_doc else 0
        big_five = user_doc.get("big_five", {}) if user_doc else {}
        current_context = user_doc.get("current_context", "") if user_doc else ""
        
        user_context = {"big_five": big_five, "current_context": current_context}
        
        result = analyze_deep_profile(req.message, prev_deep, interaction_count, user_context)
        
        update_fields = {
            "temp_deep_profile": result.get("deep_profile", {}),
            "interaction_count_deep": interaction_count + 1
        }
        
        if result.get("is_complete", False):
            update_fields["deep_profile"] = result.get("deep_profile", {})
            room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": room_id})
            if count == 0:
                save_message(room_id, "ai_assistant", "好，你在意什麼我記住了。接下來聊點生活的：最近有沒有特別想做的事或想去的地方？")

        profiles_coll.update_one(
            {"user_id": req.user_id}, 
            {"$set": update_fields}, 
            upsert=True
        )

        return {
            "status": "success", 
            "deep_profile": result.get("deep_profile"), 
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }
    else:
        raise HTTPException(status_code=400, detail="Invalid state")

@router.post("/chat/reset")
def reset_chat_state(req: ResetRequest):
    if req.state == "big_five":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count": 0, "temp_big_five": {}}}
        )
    elif req.state == "deep_profile":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count_deep": 0, "temp_deep_profile": {}}}
        )
    return {"status": "success"}

@router.get("/messages/{contact_id}")
def get_messages(contact_id: str, user_id: str):
    room_id = generate_room_id(user_id, contact_id)
    
    if contact_id == "ai_assistant":
        count = messages_coll.count_documents({"room_id": room_id})
        if count == 0:
            save_message(room_id, "ai_assistant", "哈囉，我是阿月。最近想做什麼、想去哪裡，儘管跟我說；我這個人記性很好，媒人雷達更好。")
            
    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    return {"messages": msgs}

@router.post("/direct_chat")
def direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.user_id, req.contact_id)
    save_message(room_id, req.user_id, req.message)
    if req.contact_id != "ai_assistant":
        background_tasks.add_task(observe_user_memory, req.user_id, req.message, "pair_chat")
    
    if req.contact_id == "ai_assistant":
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
        proposal_intent = classify_proposal_intent(req.message) if active_proposal_id else None
        if active_proposal_id and proposal_intent is not None:
            from models import AcceptRequest
            from routers.match import accept_match, decline_match
            action_req = AcceptRequest(user_id=req.user_id, match_id=active_proposal_id)
            try:
                result = (
                    accept_match(action_req, background_tasks)
                    if proposal_intent
                    else decline_match(action_req, background_tasks)
                )
                if proposal_intent and result.get("new_status") == "pending":
                    ai_reply = "好，我幫你去問，但不替對方做決定。先放輕鬆，媒人出馬也需要等本人點頭。"
                elif proposal_intent and result.get("new_status") == "accepted":
                    ai_reply = "雙方都點頭了！聊天室已經開好，去打聲招呼吧。"
                else:
                    ai_reply = "收到，這位先不勉強。你願意的話可以告訴我是哪裡不對味，下次我會看得更準。"
                save_message(room_id, "ai_assistant", ai_reply)
                return {"reply": ai_reply, "is_locked": False, "mode": "proposal_response", **result}
            except Exception as e:
                print(f"Proposal response failed: {e}")

        explicit_match_phrases = ("再幫我找", "再找一個", "找下一個", "再配一個", "幫我配對", "找新的對象")
        if any(phrase in req.message for phrase in explicit_match_phrases):
            active = matches_coll.find_one({
                "status": {"$in": ["draft", "pending"]},
                "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
            })
            if active:
                ai_reply = "手上這條線還沒確認完，先處理它，我再幫你找下一位。"
                status = "already_active"
            elif (user_doc or {}).get("matchmaking_in_progress"):
                ai_reply = "有有有，我正在看，先別連按門鈴啦。"
                status = "already_searching"
            else:
                profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                    "status": "queued", "source": "explicit_next", "requested_at": time.time()}}})
                background_tasks.add_task(trigger_proactive_match, req.user_id, "explicit_next", True)
                ai_reply = "好，這次是真的排進去了；有結果或找不到，我都會回來跟你說。"
                status = "queued"
            save_message(room_id, "ai_assistant", ai_reply)
            background_tasks.add_task(observe_user_memory, req.user_id, req.message, "global")
            return {"reply": ai_reply, "is_locked": False, "mode": "match_request", "match_request_status": status}

        bf = user_doc.get("big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0
        
        current_context = user_doc.get("current_context", "無") if user_doc else "無"
        current_round = interaction_count + 1
        accepted_matches = list(matches_coll.find({
            "status": "accepted", "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
        }).limit(8))
        relationship_lines = []
        for matched in accepted_matches:
            other = matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            state = (matched.get("mediator_state", {}) or {}).get("participants", {})
            relationship_lines.append({
                "other_id": other, "match_id": str(matched["_id"]),
                "match_reason": matched.get("reason", ""),
                "shared_summary": (matched.get("relationship_memory", {}) or {}).get("shared_summary", ""),
                "mediator_progress": state
            })
        relationship_context = json.dumps(relationship_lines, ensure_ascii=False)
        memory_summary = (user_doc or {}).get("profile_memory_summary", "尚無長期偏好記憶")

        sys_prompt = f"""
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style(req.user_id)}
你正在與使用者閒聊，關心他的近況。
你可以自然閒聊、聊已配對的人，也可以在使用者真的談到新生活計畫時更新近期情境。你在所有聊天室都是同一位阿月。
【背景資訊】
- 使用者的性格特質：{bf.get('summary', '未知')}
- 使用者上次紀錄的情境：{current_context}
- 阿月記得的長期偏好：{memory_summary}
- 已配對關係與阿月工作進度：{relationship_context}

對話守則：
1. 每次回覆請盡量簡短（1~2句話以內）。
2. ⚠️切換話題：你必須【非常仔細看使用者最後說了什麼】！如果使用者在最後一句話提到了全新的計畫或動態（例如：我要去某個地方），請「立刻」順著他的新話題給予強烈共鳴與追問，【絕對不要】再回頭提背景資訊裡的舊情境（例如蛋塔、咖啡等）。
3. ⚠️注意時態與事實：如果使用者表示事情「已經發生」，請如實記錄（例如「剛去過福岡玩」），絕對不能寫成「想去...」。
4. 先判斷意圖：recent_context、relationship_chat、profile_fact、casual_chat、command。提到上方已配對對象時優先是 relationship_chat。
5. 只有 recent_context 且明確包含新的生活活動或計畫時，context_should_update 才能為 true；其他意圖必須沿用舊情境。
6. 只有 recent_context 才評估媒合情境信心 0~100；活動、時間、偏好、同行意願各占 25 分，不能腦補。
7. 聊關係時可使用媒合理由、共享摘要與阿月工作進度，但不可搬運未授權私話。

請嚴格回傳以下 JSON 格式：
{{
    "reply": "你給使用者的回覆 (繁體中文)",
    "conversation_intent": "recent_context|relationship_chat|profile_fact|casual_chat|command",
    "mentioned_other_id": "已配對對象 user_id 或 null",
    "context_should_update": false,
    "context_confidence": 0.0,
    "context_summary": "只有應更新時填新摘要，否則原樣回傳舊情境",
    "match_readiness_score": 0,
    "readiness_reason": "還缺少或已掌握哪些關鍵訊號",
    "context_signals": {{
        "activity": "活動或主題 / null",
        "timing": "時間感 / null",
        "preference": "具體偏好 / null",
        "companion_intent": "是否想有人同行 / null"
    }}
}}
"""
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "使用者" if m["sender_id"] == req.user_id else "媒人阿月"
            prompt += f"{speaker}: {m['content']}\n"
        
        prompt += "\n請身為「媒人阿月」，針對對話紀錄中「使用者」的【最後一句話】，給出你的回覆："
            
        # Add the current message which is already in history because of save_message above
        
        try:
            ai_res_str = generate_chat_completion(prompt, temperature=0.6, json_output=True)
            ai_res = json.loads(ai_res_str)
            ai_reply = ai_res.get("reply", "收到！")
            is_locked = False
            intent = ai_res.get("conversation_intent", "casual_chat")
            mentioned_other_id = ai_res.get("mentioned_other_id")
            try:
                context_confidence = float(ai_res.get("context_confidence", 0))
            except (TypeError, ValueError):
                context_confidence = 0.0
            explicit_context = any(p in req.message for p in ("幫我更新近況", "把這記成近況", "更新近期情境"))
            explicit_no_memory = any(p in req.message for p in ("別記這段", "不要記", "只是閒聊"))
            context_should_update = bool(ai_res.get("context_should_update")) and intent == "recent_context" and context_confidence >= 0.85
            if explicit_context:
                context_should_update = True
                intent = "recent_context"
            if explicit_no_memory or (mentioned_other_id and not explicit_context):
                context_should_update = False
            candidate_ctx = ai_res.get("context_summary") or current_context
            context_changed = bool(context_should_update and candidate_ctx != current_context)
            new_ctx = candidate_ctx if context_changed else current_context
            old_revision = int((user_doc or {}).get("current_context_revision", 0))
            new_revision = old_revision + 1 if context_changed else old_revision
            try:
                readiness_score = max(0, min(100, int(ai_res.get("match_readiness_score", 0)))) if intent == "recent_context" else 0
            except (TypeError, ValueError):
                readiness_score = 0
            update_fields = {"match_readiness_score": readiness_score,
                "match_readiness_reason": ai_res.get("readiness_reason", ""),
                "context_signals": ai_res.get("context_signals", {}) if intent == "recent_context" else {},
                "last_conversation_intent": intent, "ai_chat_locked": False}
            increments = {"ai_chat_interaction_count": 1}
            if context_changed:
                update_fields.update({"current_context": new_ctx, "current_context_revision": new_revision,
                                      "previous_context": current_context})
            profiles_coll.update_one({"user_id": req.user_id}, {"$set": update_fields, "$inc": increments}, upsert=True)

            active_match = matches_coll.find_one({"status": {"$in": ["draft", "pending"]},
                "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]})
            last_auto_revision = (user_doc or {}).get("last_auto_match_revision")
            matching_in_progress = bool((user_doc or {}).get("matchmaking_in_progress"))
            if readiness_score >= MATCH_READINESS_THRESHOLD and not active_match and last_auto_revision != new_revision and not matching_in_progress:
                try:
                    context_embedding = get_embedding(new_ctx)
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {
                        "context_embedding": context_embedding, "last_auto_match_revision": new_revision}}, upsert=True)
                except HTTPException as e:
                    print(f"Embedding skipped in direct_chat: {e.detail}")
                background_tasks.add_task(trigger_proactive_match, req.user_id, "automatic", False)
            if not explicit_no_memory:
                background_tasks.add_task(observe_user_memory, req.user_id, req.message, "global")
                    
        except Exception as e:
            print(f"Chat error (AI): {e}")
            ai_reply = "不好意思，我現在有點短路，請稍後再試！"
            is_locked = False
            
        save_message(room_id, "ai_assistant", ai_reply)
        return {
            "reply": ai_reply,
            "is_locked": is_locked,
            "match_readiness_score": locals().get("readiness_score", 0),
            "conversation_intent": locals().get("intent", "casual_chat"),
            "mentioned_other_id": locals().get("mentioned_other_id"),
            "context_changed": locals().get("context_changed", False),
            "context_confirmation_needed": bool(locals().get("intent") == "recent_context" and 0.65 <= locals().get("context_confidence", 0) < 0.85)
        }
        
    else:
        target_doc = profiles_coll.find_one({"user_id": req.contact_id})
        target_bf = target_doc.get("big_five", {}) if target_doc else {}
        
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        sys_prompt = f"你現在扮演使用者 {req.contact_id}。你的大五人格設定為：{target_bf}。你要跟一位配對到的用戶聊天，請用符合你性格的方式回答（繁體中文），絕不能透露你是AI或語言模型。像即時聊天一樣只回 1～2 句，不長篇解釋，也不要每次都反問。"
        prompt = sys_prompt + "\n\n【對話紀錄】\n"
        for m in history:
            speaker = "對方" if m["sender_id"] == req.user_id else "你"
            prompt += f"{speaker}: {m['content']}\n"
        prompt += "你:"
            
        try:
            reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)
        except Exception as e:
            print(f"Chat error (User {req.contact_id}): {e}")
            reply = "不好意思，我現在有點短路，請稍後再試！"
        save_message(room_id, req.contact_id, reply)
        match_doc = matches_coll.find_one({
            "status": "accepted",
            "$or": [
                {"from_user": req.user_id, "to_user": req.contact_id},
                {"from_user": req.contact_id, "to_user": req.user_id}
            ]
        })
        message_count = mark_post_chat_activity(match_doc, room_id)
        if match_doc and message_count >= 6:
            background_tasks.add_task(summarize_relationship, match_doc["_id"], room_id)
        return {"reply": reply, "feedback_scheduled": bool(match_doc)}

@router.get("/contacts")
def get_contacts(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False
    
    query = {
        "status": "accepted",
        "$or": [{"from_user": user_id}, {"to_user": user_id}]
    }
    matches = list(matches_coll.find(query))
    
    contacts = [
        {"id": "ai_assistant", "name": "媒人阿月", "role": "system", "context": "懂你、牽線，也幫你探口風", "is_locked": ai_locked}
    ]
    
    for m in matches:
        other_id = m["to_user"] if m["from_user"] == user_id else m["from_user"]
        other_doc = profiles_coll.find_one({"user_id": other_id})
        ctx = other_doc.get("current_context", "交朋友") if other_doc else "交朋友"
        contacts.append({
            "id": other_id,
            "name": other_id, 
            "role": "user",
            "context": ctx
        })
        
    return {"contacts": contacts}

def generate_mediator_private_room_id(user_id: str, other_id: str):
    return f"mediator_private::{user_id}::{other_id}"

def find_accepted_match(user_id: str, other_id: str):
    return matches_coll.find_one({
        "status": "accepted",
        "$or": [
            {"from_user": user_id, "to_user": other_id},
            {"from_user": other_id, "to_user": user_id}
        ]
    })

@router.get("/mediator/private/{other_id}")
def get_mediator_private_messages(other_id: str, user_id: str):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以私訊阿月")
    room_id = generate_mediator_private_room_id(user_id, other_id)
    if messages_coll.count_documents({"room_id": room_id}) == 0:
        save_message(room_id, "ai_assistant", f"想問 {other_id} 的事就來找我；不知道的我會老實說，不搬私話。")
    unread_field = relationship_unread_field(match_doc, user_id)
    unread_count = int((match_doc.get("private_unread", {}) or {}).get(
        "from" if match_doc.get("from_user") == user_id else "to", 0
    ))
    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {unread_field: 0}})
    user_doc = profiles_coll.find_one({"user_id": user_id}) or {}
    pending = user_doc.get("pending_private_feedback") or {}
    if pending.get("other_id") != other_id:
        pending = {}
    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    return {
        "messages": msgs,
        "other_id": other_id,
        "unread_count": unread_count,
        "pending_step": pending.get("stage"),
        "probe_state": participant_probe_state(match_doc, user_id),
        "other_probe_state": participant_probe_state(match_doc, other_id),
        "mediator_tone": user_doc.get("mediator_tone", "friend")
    }

def consent_intent(message: str):
    negative = ("先不要", "不要說", "不分享", "保密", "不用透露", "不同意")
    positive = ("可以", "好啊", "同意", "透露", "跟他說", "稍微說")
    if any(word in message for word in negative):
        return False
    if any(word in message for word in positive):
        return True
    return None

def save_private_mediator_reply(room_id: str, reply: str, event_type="text", actions=None):
    message_type = "mediator_card" if actions else "text"
    save_message(
        room_id, "ai_assistant", reply, message_type=message_type,
        metadata={"event_type": event_type, "actions": actions or []}
    )

def deliver_consented_signal(match_doc: dict, feedback_user: str, requester_id: str, sentiment: str):
    if sentiment == "positive":
        queue_mediator_event(
            requester_id,
            f"偷偷跟你說，{feedback_user} 對你的印象不錯，可以自然往前一步。",
            "probe_result", match_id=str(match_doc["_id"]), other_id=feedback_user
        )
    elif sentiment == "negative":
        queue_mediator_event(
            requester_id,
            "我探過了，你們現在的節奏沒完全對上；不是誰不好，我幫你把台階顧好。",
            "gentle_closure", match_id=str(match_doc["_id"]), other_id=feedback_user
        )

def request_relationship_probe(user_id: str, other_id: str, force: bool = False):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以探口風")
    target_state = participant_probe_state(match_doc, other_id)
    status = target_state.get("status", "idle")
    now, count = time.time(), int(match_doc.get("shared_message_count", 0))
    if status in {"queued", "awaiting_sentiment", "awaiting_consent"} and float(target_state.get("asked_at", now)) > now - PROBE_PENDING_TTL:
        return {"status": "already_pending", "reply": "我正在問，有消息會來這裡敲你。"}
    new_messages = count - int(target_state.get("message_count_snapshot", 0))
    if status == "completed" and new_messages < 6:
        return {"status": "recently_completed", "reply": "我才剛問過，先讓你們多聊一點；有新的互動我再幫你探。"}
    if status == "completed" and now < float(target_state.get("cooldown_until", 0)) and not force:
        return {"status": "needs_confirmation", "reply": "今天才問過一次；你真的還想再問，我可以幫你再敲，但別把人家問成口試啦。"}
    _, _, _, cooldown = probe_policy(user_id)
    state = {"status": "queued", "trigger": "manual", "requester_id": user_id,
             "asked_at": now, "message_count_snapshot": count, "cooldown_until": now + cooldown}
    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {participant_probe_field(match_doc, other_id): state}})
    queue_mediator_event(other_id, "剛剛聊起來感覺怎樣？", "feedback_request",
        match_id=str(match_doc["_id"]), other_id=user_id, origin="probe", requester_id=user_id)
    return {"status": "started", "reply": "好，我私下問問；他沒點頭前，我什麼都不會帶回來。"}

@router.post("/mediator/probe")
def mediator_probe(req: MediatorProbeRequest):
    return request_relationship_probe(req.user_id, req.other_id, req.force)

@router.post("/mediator/private")
def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以私訊阿月")

    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
    save_message(room_id, req.user_id, req.message)
    background_tasks.add_task(observe_user_memory, req.user_id, req.message, "relationship_private", str(match_doc["_id"]))
    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    pending = user_doc.get("pending_private_feedback") or {}
    pending_matches = pending.get("match_id") == str(match_doc["_id"]) and pending.get("other_id") == req.other_id
    is_probe_command = any(phrase in req.message for phrase in ("喜歡我", "有好感", "怎麼看我", "對我的感覺", "探口風", "口風", "探探", "幫我問"))
    if pending_matches and pending.get("stage") == "sentiment" and is_probe_command:
        probe_result = request_relationship_probe(req.user_id, req.other_id, False)
        save_private_mediator_reply(room_id, probe_result["reply"])
        return {"reply": probe_result["reply"], "pending_step": "sentiment", "probe_status": probe_result["status"]}

    if pending_matches and pending.get("stage") == "sentiment":
        sentiment = classify_feedback(req.message)
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {f"private_feedback.{req.user_id}": {
                "sentiment": sentiment, "share_consent": None, "updated_at": time.time()
            }}}
        )
        pending["stage"] = "consent"
        pending["sentiment"] = sentiment
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
            participant_probe_field(match_doc, req.user_id) + ".status": "awaiting_consent",
            participant_probe_field(match_doc, req.user_id) + ".sentiment": sentiment}})
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"pending_private_feedback": pending}}
        )
        reply = "懂。要不要我只跟他透露大方向？"
        actions = [
            {"label": "可以稍微說", "value": "可以，只說大方向"},
            {"label": "先不要", "value": "先不要，替我保密"}
        ]
        save_private_mediator_reply(room_id, reply, "feedback_consent_request", actions)
        return {"reply": reply, "pending_step": "consent", "actions": actions}

    if pending_matches and pending.get("stage") == "consent":
        consent = consent_intent(req.message)
        if consent is None:
            reply = "我先不動；你只要告訴我『可以稍微說』或『先不要』就好。"
            save_private_mediator_reply(room_id, reply)
            return {"reply": reply, "pending_step": "consent"}
        sentiment = pending.get("sentiment", "neutral")
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {
                f"private_feedback.{req.user_id}.share_consent": consent,
                f"private_feedback.{req.user_id}.updated_at": time.time()
            }}
        )
        profiles_coll.update_one({"user_id": req.user_id}, {"$unset": {"pending_private_feedback": ""}})
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
            participant_probe_field(match_doc, req.user_id) + ".status": "completed",
            participant_probe_field(match_doc, req.user_id) + ".sentiment": sentiment,
            participant_probe_field(match_doc, req.user_id) + ".share_consent": consent,
            participant_probe_field(match_doc, req.user_id) + ".completed_at": time.time()}})
        requester_id = pending.get("requester_id")
        if consent and requester_id and requester_id == req.other_id:
            deliver_consented_signal(match_doc, req.user_id, requester_id, sentiment)
        refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or {}
        feedback = refreshed.get("private_feedback", {}) or {}
        mine = feedback.get(req.user_id, {}) or {}
        theirs = feedback.get(req.other_id, {}) or {}
        if (mine.get("sentiment") == "positive" and mine.get("share_consent") is True
                and theirs.get("sentiment") == "positive" and theirs.get("share_consent") is True):
            queue_mediator_event(
                req.user_id, f"欸，{req.other_id} 對你的印象也很好，可以放心約下一次。",
                "mutual_interest", match_id=str(match_doc["_id"]), other_id=req.other_id
            )
        reply = "好，我只說大方向，不搬你的原話。" if consent else "好，這段留在我們之間。"
        save_private_mediator_reply(room_id, reply)
        return {"reply": reply, "pending_step": None}

    asks_about_feelings = is_probe_command
    if asks_about_feelings:
        probe_result = request_relationship_probe(req.user_id, req.other_id, False)
        reply = probe_result["reply"]
    else:
        other_doc = profiles_coll.find_one(
            {"user_id": req.other_id},
            {"_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1, "big_five.summary": 1}
        ) or {}
        feedback = match_doc.get("private_feedback", {}) or {}
        consented_signal = None
        other_feedback = feedback.get(req.other_id, {}) or {}
        if other_feedback.get("share_consent") is True:
            consented_signal = other_feedback.get("sentiment")
        safe_profile = {
            "user_id": other_doc.get("user_id"),
            "公開興趣": other_doc.get("initial_interest"),
            "近期公開情境": other_doc.get("current_context"),
            "公開性格簡述": (other_doc.get("big_five") or {}).get("summary"),
            "媒合理由": match_doc.get("reason") if match_doc.get("from_user") == req.user_id else match_doc.get("receiver_reason", match_doc.get("reason")),
            "共享聊天摘要": match_doc.get("relationship_memory", {}),
            "阿月記得的使用者偏好": user_doc.get("profile_memory_summary", ""),
            "阿月目前助攻進度": {"自己": participant_probe_state(match_doc, req.user_id),
                                "對方": participant_probe_state(match_doc, req.other_id)},
            "對方同意分享的口風": consented_signal
        }
        prompt = f"""
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style(req.user_id)}
你正在 {req.user_id} 與 {req.other_id} 的關係悄悄話中回答。
只能依下列資料回答；不知道就直接說不知道，不引用私人原話、不補故事。
可分享資料：{json.dumps(safe_profile, ensure_ascii=False)}
使用者問：{req.message}
"""
        try:
            reply = generate_chat_completion(prompt, temperature=0.35, json_output=False)
        except Exception as e:
            print(f"Mediator private chat error: {e}")
            reply = "這題我不知道，別讓我瞎猜；你直接問他反而最自然。"

    save_private_mediator_reply(room_id, reply)
    return {"reply": reply, "pending_step": None}

@router.get("/proactive_check")
def proactive_check(user_id: str):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}

    queue_due_feedback(user_id)

    # Deliver queued events. Relationship events stay inside the matching pair room.
    popped = profiles_coll.find_one_and_update(
        {"user_id": user_id, "mediator_inbox.0": {"$exists": True}},
        {"$pop": {"mediator_inbox": -1}},
        projection={"mediator_inbox": 1},
        return_document=ReturnDocument.BEFORE
    )
    if popped and popped.get("mediator_inbox"):
        event = popped["mediator_inbox"][0]
        event_type = event.get("type", "mediator_message")
        other_id = event.get("other_id")
        event_match = None
        if event.get("match_id"):
            try:
                from bson.objectid import ObjectId
                event_match = matches_coll.find_one({"_id": ObjectId(event.get("match_id"))})
            except Exception:
                event_match = None
        if not event_match and other_id:
            event_match = find_accepted_match(user_id, other_id)
        relationship_private = bool(
            other_id and event_match and event_match.get("status") == "accepted"
            and (event_type in RELATIONSHIP_EVENT_TYPES or event.get("match_id"))
        )
        message_metadata = {
            "event_type": event_type,
            "match_id": event.get("match_id"),
            "other_id": other_id,
            "proposal_role": event.get("proposal_role"),
            "matches": event.get("matches", []),
            "actions": event.get("actions", [])
        }
        if relationship_private:
            room_id = generate_mediator_private_room_id(user_id, other_id)
            if event_type == "feedback_request":
                state = participant_probe_state(event_match, user_id)
                asked_at = float(state.get("asked_at", 0))
                duplicate = asked_at and messages_coll.find_one({"room_id": room_id,
                    "metadata.event_type": "feedback_request", "timestamp": {"$gte": asked_at - 1}})
                if duplicate and state.get("status") in {"awaiting_sentiment", "awaiting_consent"}:
                    return {"has_new": False, "deduplicated": True}
            delivered_message = event.get("message", "我有悄悄話。")
            if event_type == "feedback_request":
                delivered_message = "剛剛聊起來感覺怎樣？"
                message_metadata["actions"] = []
            save_message(
                room_id, "ai_assistant", delivered_message,
                message_type="mediator_card" if message_metadata["actions"] else "text",
                metadata=message_metadata
            )
            unread_field = relationship_unread_field(event_match, user_id)
            updated_match = matches_coll.find_one_and_update(
                {"_id": event_match["_id"]}, {"$inc": {unread_field: 1}},
                return_document=ReturnDocument.AFTER
            ) or event_match
            role = "from" if event_match.get("from_user") == user_id else "to"
            unread_count = int((updated_match.get("private_unread", {}) or {}).get(role, 1))
            if event_type == "feedback_request":
                requester_id = event.get("requester_id") or participant_probe_state(event_match, user_id).get("requester_id")
                profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_private_feedback": {
                    "match_id": str(event_match["_id"]), "other_id": other_id,
                    "stage": "sentiment", "origin": event.get("origin", "auto"), "requester_id": requester_id}}})
                matches_coll.update_one({"_id": event_match["_id"]}, {"$set": {
                    participant_probe_field(event_match, user_id) + ".status": "awaiting_sentiment",
                    participant_probe_field(event_match, user_id) + ".asked_at": time.time()}})
            return {
                "has_new": True, "surface": "relationship_private", "other_id": other_id,
                "unread_count": unread_count, "message": delivered_message,
                "type": event_type, "metadata": message_metadata
            }

        room_id = generate_room_id(user_id, "ai_assistant")
        message_type = "mediator_card" if event_type in {"match_proposal", "incoming_match_interest"} else "text"
        save_message(room_id, "ai_assistant", event.get("message", "我有件事想跟你聊聊。"), message_type=message_type, metadata=message_metadata)
        if event_type in {"match_proposal", "incoming_match_interest"}:
            profiles_coll.update_one(
                {"user_id": user_id},
                {"$set": {"active_match_proposal_id": event.get("match_id"), "ai_chat_locked": False}}
            )
        return {
            "has_new": True, "surface": "global_mediator", "message": event.get("message"),
            "type": event_type, "matches": event.get("matches", []),
            "metadata": message_metadata, "debug_info": event.get("debug_info", [])
        }

    freq_str = str(user_doc.get("proactive_frequency", "none"))
    if freq_str == "none":
        return {"has_new": False}
        
    try:
        freq_seconds = int(freq_str)
    except ValueError:
        return {"has_new": False}
        
    last_time = user_doc.get("last_proactive_time", 0)
    current_time = time.time()
    
    if current_time - last_time >= freq_seconds:
        # Time to send a proactive message
        # Update time first to prevent duplicate triggers
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"last_proactive_time": current_time}})
        
        bf = user_doc.get("big_five", {})
        ctx = user_doc.get("current_context", "無特別情境")
        
        prompt = f"""
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style(user_id)}
這是你主動發起的對話，用來關心使用者最近的狀況。
使用者的性格特質是：{bf.get('summary', '未知')}
使用者上次紀錄的情境/興趣是：{ctx}

請用繁體中文，只用一句、必要時最多兩句，根據上述資訊主動開話題關心對方。
【極度重要】：你必須針對他「上次的情境」進行自然的「後續追問」（例如：上次聽說你想去非洲，後來去成了嗎？有沒有看到大象？）。如果沒有特別情境，就隨機找個輕鬆的話題閒聊。語氣自然、像朋友一樣。
"""
        try:
            ai_reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)
            room_id = generate_room_id(user_id, "ai_assistant")
            save_message(room_id, "ai_assistant", ai_reply)
            profiles_coll.update_one({"user_id": user_id}, {"$set": {"ai_chat_locked": False, "ai_chat_interaction_count": 0}})
            return {"has_new": True, "message": ai_reply}
        except Exception as e:
            print(f"Proactive chat error: {e}")
            return {"has_new": False}
            
    return {"has_new": False}
