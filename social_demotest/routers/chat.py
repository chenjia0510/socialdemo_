import json
import time
import os
import re
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pymongo import ReturnDocument
from models import (
    ChatRequest, DirectChatRequest, MediatorPrivateRequest, MediatorProbeRequest,
    RelationshipGameRequest, RelationshipQuizAnswerRequest, ResetRequest,
)
from database import profiles_coll, messages_coll, matches_coll
from services.ai_service import analyze_big_five, analyze_deep_profile, get_embedding, generate_chat_completion
from services.chat_service import generate_room_id, save_message
from services.memory_service import observe_user_memory, get_user_graph_memories
from services.mediator_event_service import claim_next_mediator_event, queue_mediator_event

router = APIRouter(prefix="/api", tags=["Chat"])
MATCH_READINESS_THRESHOLD = 75
FEEDBACK_COOLDOWN_SECONDS = 120
PROBE_PENDING_TTL = 72 * 3600
PROBE_IN_FLIGHT_STATUSES = {
    "queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"
}
QUIZ_TTL_SECONDS = 7 * 86400
QUIZ_QUESTIONS = [
    {
        "id": "weekend",
        "text": "理想週末比較像哪一種？",
        "options": ["慢慢吃早餐", "出門走走", "臨時小冒險"],
    },
    {
        "id": "first_meet",
        "text": "第一次單獨出去，你會選？",
        "options": ["咖啡店", "散步", "一起吃飯"],
    },
    {
        "id": "chat_rhythm",
        "text": "舒服的聊天節奏是？",
        "options": ["看到就回", "有空集中聊", "偶爾語音"],
    },
]

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
    "gentle_closure", "mutual_interest", "probe_question",
    "date_coordination_request", "date_coordination_result"
}

PROBE_QUESTIONS = {
    "sentiment": "剛剛聊起來感覺怎樣？",
    "fun_fact": "欸，說一個最近發生、會讓人更認識你的有趣小事？我可能會把大方向帶回去喔。",
    "weekend": "你最近理想的週末長什麼樣？我可能會把這個大方向帶回去喔。",
    "conversation_hook": "你最近有哪個話題一聊就會停不下來？我可能會拿去幫你們開話題喔。",
    "availability": "最近如果有人約你出去，你比較偏好平日晚上還是週末？我只會帶回大方向。",
}
LOW_SENSITIVITY_PROBES = {"fun_fact", "weekend", "conversation_hook", "availability"}

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

def trigger_proactive_match(user_id: str, source: str = "automatic", force_new: bool = False):
    from routers.match import create_proactive_match_proposal
    create_proactive_match_proposal(user_id, source=source, force_new=force_new)


def is_explicit_match_request(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    phrases = (
        "再幫我找", "再找一個", "找下一個", "再配一個", "幫我配對", "找新的對象",
        "幫我配下一位", "配下一位", "找第二個", "配第二個", "再介紹一個", "下一位",
    )
    return any(phrase in compact for phrase in phrases) or bool(
        re.search(r"(幫我|再|繼續).{0,5}(找|配|介紹).{0,5}(人|對象|一位|一個)", compact)
    )


def latest_shared_chat(match_doc: dict, limit: int = 16):
    if not match_doc:
        return []
    room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1}
    ).sort("timestamp", -1).limit(limit))[::-1]
    return [{"sender_id": item.get("sender_id"), "content": item.get("content", "")} for item in history]


def relevant_graph_memories(user_id: str, message: str, limit: int = 9):
    memories = get_user_graph_memories(user_id, 20)
    compact = re.sub(r"\s+", "", message.lower())
    grams = {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}

    def relevance(item):
        haystack = " ".join(str(item.get(key, "")).lower() for key in ("key", "label", "category"))
        overlap = sum(1 for gram in grams if gram and gram in haystack)
        return (overlap, float(item.get("confidence", 0)), float(item.get("last_seen_at", 0)))

    related = sorted(memories, key=relevance, reverse=True)
    directly_related = [item for item in related if relevance(item)[0] > 0][:6]
    selected = directly_related[:]
    for item in sorted(memories, key=lambda value: (
        float(value.get("confidence", 0)), float(value.get("last_seen_at", 0))
    ), reverse=True):
        if item not in selected and len(selected) < limit:
            selected.append(item)
    return selected


def mediator_profile_context(user_id: str, message: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {
        "_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1,
        "big_five": 1, "deep_profile": 1,
    }) or {}
    return {
        "owner_user_id": user_id,
        "initial_interest": doc.get("initial_interest"),
        "current_context": doc.get("current_context"),
        "big_five": doc.get("big_five", {}),
        "deep_profile": doc.get("deep_profile", {}),
        "graph_memories": relevant_graph_memories(user_id, message),
    }


def has_context_signal(value) -> bool:
    if value is None or value is False:
        return False
    text = str(value).strip().lower()
    return text not in {
        "", "null", "none", "無", "未知", "不確定", "false",
        "未提及", "沒有提到", "尚未提到", "未說明"
    }


def deterministic_readiness(intent: str, signals: dict) -> tuple[int, list[str]]:
    if intent != "recent_context" or not isinstance(signals, dict):
        return 0, ["activity", "timing", "preference", "companion_intent"]
    weights = {"activity": 40, "timing": 20, "preference": 20, "companion_intent": 20}
    available = {key: has_context_signal(signals.get(key)) for key in weights}
    companion_text = str(signals.get("companion_intent", "")).lower()
    if any(word in companion_text for word in ("不想", "不要", "自己去", "不需要", "沒有要找")):
        available["companion_intent"] = False
    score = sum(weight for key, weight in weights.items() if available[key])
    missing = [key for key in weights if not available[key]]
    return score, missing


def is_relationship_query(message: str, mentioned_ids: list[str]) -> bool:
    if mentioned_ids:
        return True
    keywords = (
        "配到的人", "配對的人", "哪一位", "哪位", "誰比較", "比較好", "這個人",
        "他的名字", "他的id", "她的名字", "她的id", "你知道我配到誰", "目前有誰",
        "他喜歡", "她喜歡", "跟他", "跟她"
    )
    compact = re.sub(r"\s+", "", message.lower())
    return any(keyword in compact for keyword in keywords)


def unsupported_relationship_fact(
    message: str,
    evidence_catalog: dict,
    evidence_owners: dict | None = None,
    subject_ids: list[str] | None = None,
) -> str | None:
    compact = re.sub(r"\s+", "", message)
    patterns = (
        r"喜歡(.{1,20}?)(?:的是哪|的是誰|是哪一位|是哪位|是誰)",
        r"(?:誰|哪一位|哪位)喜歡(.{1,20}?)(?:\?|？|$)",
    )
    allowed_subjects = set(subject_ids or [])
    evidence_text = " ".join(
        str(value) for key, value in evidence_catalog.items()
        if not allowed_subjects or not evidence_owners
        or evidence_owners.get(key) in allowed_subjects
    ).lower()
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            fact = match.group(1).strip("的？?")
            if fact and fact.lower() not in evidence_text:
                return fact
    return None


def validate_relationship_claims(
    claims: list,
    accepted_ids: set[str],
    evidence_catalog: dict,
    evidence_owners: dict,
) -> tuple[set[str], list[str]]:
    valid_subjects = set()
    valid_evidence = []
    for claim in claims or []:
        subject_id = claim.get("subject_user_id")
        if subject_id not in accepted_ids:
            continue
        claim_evidence = [
            evidence_id for evidence_id in (claim.get("evidence_ids") or [])
            if evidence_id in evidence_catalog
            and evidence_owners.get(evidence_id) == subject_id
        ]
        if claim_evidence:
            valid_subjects.add(subject_id)
            for evidence_id in claim_evidence:
                if evidence_id not in valid_evidence:
                    valid_evidence.append(evidence_id)
    return valid_subjects, valid_evidence


def grounded_relationship_fallback(relationships: list[dict], compare: bool = False) -> str:
    if not relationships:
        return "我目前找不到已完成配對的對象，先不亂報名字。"
    if compare and len(relationships) >= 2:
        ranked = sorted(
            relationships,
            key=lambda item: (
                float((item.get("score_breakdown") or {}).get("total", 0) or 0),
                int(item.get("shared_message_count", 0) or 0)
            ),
            reverse=True
        )
        best = ranked[0]
        evidence_notes = []
        for item in ranked[:3]:
            evidence = item.get("shared_summary") or item.get("public_context") or "目前可確認的互動還不多"
            evidence = str(evidence).strip()
            if len(evidence) > 72:
                evidence = evidence[:72].rstrip("。！？!?；;") + "…"
            evidence_notes.append(f"@{item['other_id']}：{evidence.rstrip('。！？!?；;')}")
        return (
            "只看目前有紀錄的資料，" + "；".join(evidence_notes)
            + f"。我會先繼續觀察 @{best['other_id']}，但不會替你把答案說死。"
        )
    ids = "、".join("@" + item["other_id"] for item in relationships)
    return f"我目前能確認的已配對對象是 {ids}；其他興趣或經歷沒有紀錄的，我不會替他們補故事。"


def choose_probe_kind(match_doc: dict, requested_kind: str | None = None) -> str:
    if requested_kind in PROBE_QUESTIONS:
        return requested_kind
    recent = [item.get("kind") for item in (match_doc.get("probe_history", []) or [])[-5:]]
    for kind in ("fun_fact", "conversation_hook", "weekend", "availability", "sentiment"):
        if (not recent or kind != recent[-1]) and kind not in recent[-3:]:
            return kind
    return "fun_fact"


def normalize_date_answer(stage: str, message: str):
    if stage == "availability":
        choices = [
            value for value in ("平日晚上", "週末早上", "週末下午", "週末晚上")
            if value in message
        ]
        return choices or ["時間再約"]
    if stage == "activity":
        choices = [value for value in ("咖啡", "散步", "電影", "吃飯", "展覽") if value in message]
        return choices or ["輕鬆聊天"]
    if any(word in message for word in ("一千以上", "1000以上", "不限")):
        return "1000以上"
    if any(word in message for word in ("五百內", "500內", "省一點")):
        return "500內"
    return "500到1000"


def is_date_cancellation(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    return any(phrase in compact for phrase in (
        "不要了", "取消", "先不用", "不用了", "不想約", "停止協調",
        "不要協調", "先不要約", "這次算了",
    ))


def date_overlap(first: dict, second: dict):
    times = [item for item in first.get("availability", []) if item in second.get("availability", [])]
    activities = [item for item in first.get("activity", []) if item in second.get("activity", [])]
    return {
        "time": times[0] if times else None,
        "activity": activities[0] if activities else None,
        "budget": first.get("budget") if first.get("budget") == second.get("budget") else "先從簡單行程開始",
    }

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
    positive_words = ("開始找", "現在找", "有興趣", "想認識", "可以", "好啊", "願意", "幫我問", "接受")
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
        if status in PROBE_IN_FLIGHT_STATUSES:
            if float(state.get("asked_at", now)) < now - PROBE_PENDING_TTL:
                matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
                    participant_probe_field(match_doc, user_id) + ".status": "expired"}})
            continue
        last_count = int(state.get("message_count_snapshot", 0))
        if state.get("completed_at") and (count - last_count < 6 or now < float(state.get("cooldown_until", 0))):
            continue
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        kind = choose_probe_kind(match_doc)
        question = PROBE_QUESTIONS[kind]
        probe_id = uuid.uuid4().hex
        probe_state = {"status": "queued", "trigger": "auto", "requester_id": None,
                       "probe_id": probe_id,
                       "kind": kind, "question": question,
                       "asked_at": now, "message_count_snapshot": count,
                       "cooldown_until": now + cooldown_seconds}
        state_field = participant_probe_field(match_doc, user_id)
        claimed = matches_coll.update_one(
            {
                "_id": match_doc["_id"],
                "$or": [
                    {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES)}},
                    {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
                ],
            },
            {"$set": {participant_probe_field(match_doc, user_id): probe_state},
             "$push": {"probe_history": {
                 "probe_id": probe_id,
                 "kind": kind, "asked_to": user_id, "asked_at": now,
                 "status": "queued", "trigger": "auto"
             }}}
        )
        if not claimed.modified_count:
            continue
        queue_mediator_event(
            user_id, question, "probe_question", match_id=str(match_doc["_id"]),
            other_id=other_id, origin="auto", probe_kind=kind, probe_id=probe_id
        )
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
    user_doc = profiles_coll.find_one({"user_id": user_id})
    active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
    return {"messages": msgs, "active_match_proposal_id": active_proposal_id}

@router.post("/direct_chat")
def direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.user_id, req.contact_id)
    requested_mentions = []
    for other_id in (req.mentioned_other_ids or []) + ([req.mentioned_other_id] if req.mentioned_other_id else []):
        if other_id and other_id not in requested_mentions:
            requested_mentions.append(other_id)
    display_message = req.message
    if req.contact_id == "ai_assistant" and requested_mentions:
        prefix = " ".join("@" + other_id for other_id in requested_mentions)
        display_message = f"{prefix} {req.message}".strip()
    save_message(room_id, req.user_id, display_message)
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_user_activity_at": time.time()}},
        upsert=True,
    )
    if req.contact_id != "ai_assistant":
        background_tasks.add_task(observe_user_memory, req.user_id, req.message, "pair_chat")
    
    if req.contact_id == "ai_assistant":
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)
        history = list(history_cursor)[::-1]
        
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        match_search = (user_doc or {}).get("match_search", {}) or {}
        if match_search.get("status") == "awaiting_confirmation":
            search_intent = classify_proposal_intent(req.message)
            if search_intent is not None:
                if search_intent:
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                        "status": "queued", "source": match_search.get("source", "explicit_next"),
                        "requested_at": time.time()
                    }}})
                    background_tasks.add_task(
                        trigger_proactive_match, req.user_id,
                        match_search.get("source", "explicit_next"), True
                    )
                    ai_reply = "好，我現在真的開始翻名單；有找到或這輪沒有，我都會回來說。"
                    status = "queued"
                else:
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                        "status": "cancelled", "source": match_search.get("source", "explicit_next"),
                        "updated_at": time.time()
                    }}})
                    ai_reply = "好，那這次先不找。你想開始時再叫我。"
                    status = "cancelled"
                save_message(room_id, "ai_assistant", ai_reply)
                return {
                    "reply": ai_reply, "is_locked": False, "mode": "match_confirmation",
                    "match_request_status": status
                }
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

        if is_explicit_match_request(req.message):
            from routers.match import reconcile_match_state
            active = reconcile_match_state(req.user_id)
            if active:
                ai_reply = "手上這條線還沒確認完，先處理它，我再幫你找下一位。"
                status = "already_active"
            elif (user_doc or {}).get("matchmaking_in_progress"):
                ai_reply = "有有有，我正在看，先別連按門鈴啦。"
                status = "already_searching"
            else:
                profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                    "status": "awaiting_confirmation", "source": "explicit_next",
                    "updated_at": time.time()}}})
                ai_reply = "要我現在幫你翻翻名單嗎？你回「開始找」，我才會真的去找。"
                status = "awaiting_confirmation"
            save_message(room_id, "ai_assistant", ai_reply)
            background_tasks.add_task(observe_user_memory, req.user_id, req.message, "global")
            return {"reply": ai_reply, "is_locked": False, "mode": "match_request", "match_request_status": status}

        bf = user_doc.get("big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0
        
        current_context = user_doc.get("current_context", "無") if user_doc else "無"
        current_round = interaction_count + 1
        accepted_matches = list(matches_coll.find({
            "status": "accepted", "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
        }))
        accepted_ids = {
            matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            for matched in accepted_matches
        }
        explicit_mentions = []
        for other_id in requested_mentions + [
            accepted_id for accepted_id in accepted_ids if accepted_id in req.message
        ]:
            if other_id in accepted_ids and other_id not in explicit_mentions:
                explicit_mentions.append(other_id)
        comparison_query = any(
            phrase in req.message for phrase in ("哪一位", "哪位", "誰比較", "比較好", "哪個比較")
        )
        relationship_query = is_relationship_query(req.message, explicit_mentions)
        relationship_lines = []
        evidence_catalog = {}
        evidence_owners = {}
        shared_message_budget = 48
        for matched in accepted_matches:
            other = matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            state = (matched.get("mediator_state", {}) or {}).get("participants", {})
            other_doc = profiles_coll.find_one(
                {"user_id": other},
                {"_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1,
                 "big_five": 1, "deep_profile": 1}
            ) or {}
            other_graph = relevant_graph_memories(other, req.message) if relationship_query else []
            shared_summary = (matched.get("relationship_memory", {}) or {}).get("shared_summary", "")
            message_limit = 16 if other in explicit_mentions else (8 if comparison_query else 0)
            message_limit = min(message_limit, shared_message_budget)
            recent_messages = latest_shared_chat(matched, message_limit) if message_limit else []
            shared_message_budget -= len(recent_messages)
            reason = matched.get("reason", "") if matched.get("from_user") == req.user_id else matched.get("receiver_reason", matched.get("reason", ""))
            evidence = {}
            for key, text in (
                (f"profile:{other}:interest", other_doc.get("initial_interest")),
                (f"profile:{other}:context", other_doc.get("current_context")),
                (f"profile:{other}:personality", (other_doc.get("big_five") or {}).get("summary")),
            ):
                if text:
                    evidence[key] = str(text)
                    evidence_owners[key] = other
            for memory in other_graph:
                memory_key = f"graph:{other}:{memory.get('key')}"
                evidence[memory_key] = (
                    f"{memory.get('stance', 'like')}:{memory.get('label', '')}"
                )
                evidence_owners[memory_key] = other
            if reason:
                reason_key = f"match:{matched['_id']}:reason"
                evidence[reason_key] = reason
                evidence_owners[reason_key] = "relationship"
            if shared_summary:
                summary_key = f"relationship:{matched['_id']}:summary"
                evidence[summary_key] = shared_summary
                evidence_owners[summary_key] = "relationship"
            for index, chat_message in enumerate(recent_messages):
                chat_key = f"relationship:{matched['_id']}:chat:{index}"
                sender_id = chat_message.get("sender_id")
                evidence[chat_key] = f"{sender_id}: {chat_message.get('content', '')}"
                evidence_owners[chat_key] = sender_id
            evidence_catalog.update(evidence)
            relationship_lines.append({
                "other_id": other, "match_id": str(matched["_id"]),
                "owner_user_id": other,
                "status": "accepted", "match_reason": reason,
                "public_interest": other_doc.get("initial_interest"),
                "public_context": other_doc.get("current_context"),
                "public_personality": (other_doc.get("big_five") or {}).get("summary"),
                "deep_profile": other_doc.get("deep_profile", {}),
                "graph_memories": other_graph,
                "score_breakdown": matched.get("score_breakdown", {}),
                "shared_summary": shared_summary,
                "shared_message_count": matched.get("shared_message_count", 0),
                "mediator_progress": state,
                "latest_shared_messages": recent_messages,
                "evidence": evidence,
                "evidence_owners": {
                    key: evidence_owners.get(key) for key in evidence
                },
            })
        active_proposals = []
        for matched in matches_coll.find({
            "status": {"$in": ["draft", "pending"]},
            "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
        }):
            other = matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            active_proposals.append({
                "other_id": other, "status": matched.get("status"),
                "label": "尚未完成配對", "match_reason": matched.get("reason", "")
            })
        relationship_context = json.dumps(relationship_lines, ensure_ascii=False)
        proposal_context = json.dumps(active_proposals, ensure_ascii=False)
        memory_summary = (user_doc or {}).get("profile_memory_summary", "尚無長期偏好記憶")

        unsupported_fact = unsupported_relationship_fact(
            req.message, evidence_catalog, evidence_owners, explicit_mentions
        )
        deterministic_relationship_reply = None
        if relationship_query and unsupported_fact:
            if relationship_lines:
                known_ids = "、".join("@" + item["other_id"] for item in relationship_lines)
                deterministic_relationship_reply = (
                    f"我找不到任何已配對資料提到「{unsupported_fact}」；"
                    f"目前能確認的對象是 {known_ids}，這件事我不替他們補故事。"
                )
            else:
                deterministic_relationship_reply = "我目前找不到已完成配對的對象，先不亂報名字。"
        elif comparison_query:
            deterministic_relationship_reply = grounded_relationship_fallback(relationship_lines, True)
        if deterministic_relationship_reply is not None:
            save_message(room_id, "ai_assistant", deterministic_relationship_reply)
            return {
                "reply": deterministic_relationship_reply,
                "is_locked": False,
                "match_readiness_score": 0,
                "match_readiness_state": "learning",
                "conversation_intent": "relationship_chat",
                "mentioned_other_ids": [item["other_id"] for item in relationship_lines],
                "context_changed": False,
                "context_confirmation_needed": False,
            }

        sys_prompt = f"""
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style(req.user_id)}
你正在與使用者閒聊，關心他的近況。
你可以自然閒聊、聊已配對的人，也可以在使用者真的談到新生活計畫時更新近期情境。你在所有聊天室都是同一位阿月。
【背景資訊】
- 使用者的性格特質：{bf.get('summary', '未知')}
- 使用者上次紀錄的情境：{current_context}
- 阿月記得的長期偏好：{memory_summary}
- 已配對關係、最新共享聊天與阿月工作進度：{relationship_context}
- 尚未完成的配對提案：{proposal_context}
- 使用者明確 @ 的對象：{explicit_mentions or '無'}

對話守則：
1. 每次回覆請盡量簡短（1~2句話以內）。
2. ⚠️切換話題：你必須【非常仔細看使用者最後說了什麼】！如果使用者在最後一句話提到了全新的計畫或動態（例如：我要去某個地方），請「立刻」順著他的新話題給予強烈共鳴與追問，【絕對不要】再回頭提背景資訊裡的舊情境（例如蛋塔、咖啡等）。
3. ⚠️注意時態與事實：如果使用者表示事情「已經發生」，請如實記錄（例如「剛去過福岡玩」），絕對不能寫成「想去...」。
4. 先判斷意圖：recent_context、relationship_chat、profile_fact、casual_chat、command。提到上方已配對對象時優先是 relationship_chat。
5. 只有 recent_context 且明確包含新的生活活動或計畫時，context_should_update 才能為 true；其他意圖必須沿用舊情境。
6. context_confidence 使用 0～1；它只表示近期情境摘要的可信度，不是給使用者看的媒合分數。
7. 聊關係時只能使用每人的 evidence 內容，不可搬運未授權私話，也不可捏造興趣、經歷或對話。
8. 回答已配對對象時必須使用精確的 @user_id，不可只叫「穩重哥」等綽號。
9. 比較多人時用「依目前紀錄，我比較看好」等保留語氣，不替使用者做絕對判斷。
10. draft/pending 必須稱為「尚未完成配對」，不能混入 accepted 名單。
11. 每個關於某位對象的事實都必須放入 relationship_claims；subject_user_id 必須等於 evidence_owners 中的擁有者。使用者自己說的話不能當作對方的偏好。

請嚴格回傳以下 JSON 格式：
{{
    "reply": "你給使用者的回覆 (繁體中文)",
    "conversation_intent": "recent_context|relationship_chat|profile_fact|casual_chat|command",
    "mentioned_other_ids": ["本次回答實際提到的 accepted user_id"],
    "evidence_ids": ["本次關係回答實際使用的 evidence key"],
    "relationship_claims": [
        {{
            "subject_user_id": "這項事實描述的 accepted user_id",
            "evidence_ids": ["只放 evidence_owners 屬於該 user_id 的 key"]
        }}
    ],
    "context_should_update": false,
    "context_confidence": 0.0,
    "context_summary": "只有應更新時填新摘要，否則原樣回傳舊情境",
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
            ai_res_str = generate_chat_completion(
                prompt, temperature=0.2 if relationship_query else 0.6, json_output=True
            )
            ai_res = json.loads(ai_res_str)
            ai_reply = ai_res.get("reply", "收到！")
            is_locked = False
            intent = ai_res.get("conversation_intent", "casual_chat")
            referenced_user_ids = []
            for other_id in explicit_mentions + (ai_res.get("mentioned_other_ids") or []):
                if other_id in accepted_ids and other_id not in referenced_user_ids:
                    referenced_user_ids.append(other_id)
            valid_claim_subjects, evidence_ids = validate_relationship_claims(
                ai_res.get("relationship_claims") or [],
                accepted_ids,
                evidence_catalog,
                evidence_owners,
            )
            if not relationship_query:
                evidence_ids = [
                    evidence_id for evidence_id in (ai_res.get("evidence_ids") or [])
                    if evidence_id in evidence_catalog
                ]
            try:
                context_confidence = float(ai_res.get("context_confidence", 0))
            except (TypeError, ValueError):
                context_confidence = 0.0
            if context_confidence > 1:
                context_confidence /= 100
            context_confidence = max(0.0, min(1.0, context_confidence))
            explicit_context = any(p in req.message for p in ("幫我更新近況", "把這記成近況", "更新近期情境"))
            explicit_no_memory = any(p in req.message for p in ("別記這段", "不要記", "只是閒聊"))
            context_should_update = bool(ai_res.get("context_should_update")) and intent == "recent_context" and context_confidence >= 0.85
            if explicit_context:
                context_should_update = True
                intent = "recent_context"
            if explicit_no_memory or ((explicit_mentions or referenced_user_ids) and not explicit_context):
                context_should_update = False
            candidate_ctx = ai_res.get("context_summary") or current_context
            context_changed = bool(context_should_update and candidate_ctx != current_context)
            new_ctx = candidate_ctx if context_changed else current_context
            old_revision = int((user_doc or {}).get("current_context_revision", 0))
            new_revision = old_revision + 1 if context_changed else old_revision
            context_signals = ai_res.get("context_signals", {}) if intent == "recent_context" else {}
            readiness_score, missing_signals = deterministic_readiness(intent, context_signals)
            signal_labels = {
                "activity": "想做的事", "timing": "時間", "preference": "偏好",
                "companion_intent": "是否想找人同行"
            }
            readiness_reason = (
                "阿月已經有方向" if readiness_score >= MATCH_READINESS_THRESHOLD
                else "還想知道：" + "、".join(signal_labels[key] for key in missing_signals[:2])
            )
            update_fields = {"match_readiness_score": readiness_score,
                "match_readiness_reason": readiness_reason,
                "context_signals": context_signals,
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
            if (context_changed and readiness_score >= MATCH_READINESS_THRESHOLD and not active_match
                    and last_auto_revision != new_revision and not matching_in_progress):
                try:
                    context_embedding = get_embedding(new_ctx)
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {
                        "context_embedding": context_embedding, "last_auto_match_revision": new_revision}}, upsert=True)
                except HTTPException as e:
                    print(f"Embedding skipped in direct_chat: {e.detail}")
                profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                    "status": "awaiting_confirmation", "source": "automatic",
                    "updated_at": time.time()
                }}})
                ai_reply = (
                    ai_reply.rstrip()
                    + "\n\n我現在大概知道要找什麼人了。要我開始翻名單嗎？"
                )
            compare_grounding_ok = (
                len(referenced_user_ids) >= min(2, len(relationship_lines))
                and all(f"@{other_id}" in ai_reply for other_id in referenced_user_ids)
                and all(other_id in valid_claim_subjects for other_id in referenced_user_ids)
            )
            relationship_grounding_ok = (
                bool(referenced_user_ids)
                and all(f"@{other_id}" in ai_reply for other_id in referenced_user_ids)
                and all(other_id in valid_claim_subjects for other_id in referenced_user_ids)
            )
            if relationship_query and not (
                compare_grounding_ok if comparison_query and len(relationship_lines) > 1
                else relationship_grounding_ok
            ):
                ai_reply = grounded_relationship_fallback(relationship_lines, comparison_query)
                referenced_user_ids = [item["other_id"] for item in relationship_lines]
            unsupported_fact = unsupported_relationship_fact(
                req.message, evidence_catalog, evidence_owners, explicit_mentions
            )
            if relationship_query and unsupported_fact:
                known_ids = "、".join("@" + item["other_id"] for item in relationship_lines)
                ai_reply = (
                    f"我找不到任何已配對資料提到「{unsupported_fact}」；"
                    f"目前能確認的對象是 {known_ids}，這件事我不替他們補故事。"
                )
            if comparison_query and not unsupported_fact:
                ai_reply = grounded_relationship_fallback(relationship_lines, True)
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
            "match_readiness_state": (
                "ready" if locals().get("readiness_score", 0) >= MATCH_READINESS_THRESHOLD else "learning"
            ),
            "conversation_intent": locals().get("intent", "casual_chat"),
            "mentioned_other_ids": locals().get("referenced_user_ids", []),
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
    pending_date = user_doc.get("pending_date_coordination") or {}
    if pending_date.get("other_id") != other_id:
        pending_date = {}
    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    return {
        "messages": msgs,
        "other_id": other_id,
        "unread_count": unread_count,
        "pending_step": pending.get("stage") or (
            "date_" + pending_date.get("stage") if pending_date.get("stage") else None
        ),
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

def request_relationship_probe(
    user_id: str, other_id: str, force: bool = False, requested_kind: str | None = None
):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以探口風")
    target_state = participant_probe_state(match_doc, other_id)
    status = target_state.get("status", "idle")
    now, count = time.time(), int(match_doc.get("shared_message_count", 0))
    if status in PROBE_IN_FLIGHT_STATUSES and float(target_state.get("asked_at", now)) > now - PROBE_PENDING_TTL:
        return {"status": "already_pending", "reply": "我正在問，有消息會來這裡敲你。"}
    new_messages = count - int(target_state.get("message_count_snapshot", 0))
    if status == "completed" and new_messages < 6:
        return {"status": "recently_completed", "reply": "我才剛問過，先讓你們多聊一點；有新的互動我再幫你探。"}
    if status == "completed" and now < float(target_state.get("cooldown_until", 0)) and not force:
        return {"status": "needs_confirmation", "reply": "今天才問過一次；你真的還想再問，我可以幫你再敲，但別把人家問成口試啦。"}
    _, _, _, cooldown = probe_policy(user_id)
    kind = choose_probe_kind(match_doc, requested_kind)
    question = PROBE_QUESTIONS[kind]
    probe_id = uuid.uuid4().hex
    state = {"status": "queued", "trigger": "manual", "requester_id": user_id,
             "probe_id": probe_id,
             "kind": kind, "question": question,
             "asked_at": now, "message_count_snapshot": count, "cooldown_until": now + cooldown}
    state_field = participant_probe_field(match_doc, other_id)
    claimed = matches_coll.update_one(
        {
            "_id": match_doc["_id"],
            "$or": [
                {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES)}},
                {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
            ],
        },
        {"$set": {participant_probe_field(match_doc, other_id): state},
         "$push": {"probe_history": {
             "probe_id": probe_id,
             "kind": kind, "asked_to": other_id, "asked_at": now, "status": "queued",
             "trigger": "manual", "requester_id": user_id
         }}}
    )
    if not claimed.modified_count:
        return {"status": "already_pending", "reply": "我正在問，有消息會來這裡敲你。"}
    queue_mediator_event(
        other_id, question, "probe_question", match_id=str(match_doc["_id"]),
        other_id=user_id, origin="probe", requester_id=user_id, probe_kind=kind,
        probe_id=probe_id
    )
    return {"status": "started", "kind": kind,
            "reply": "好，我換個自然一點的角度問，不會每次都像感情問卷。"}

@router.post("/mediator/probe")
def mediator_probe(req: MediatorProbeRequest):
    return request_relationship_probe(req.user_id, req.other_id, req.force, req.kind)

@router.post("/mediator/private")
def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以私訊阿月")

    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
    save_message(room_id, req.user_id, req.message)
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_user_activity_at": time.time()}},
        upsert=True,
    )
    background_tasks.add_task(observe_user_memory, req.user_id, req.message, "relationship_private", str(match_doc["_id"]))
    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    pending = user_doc.get("pending_private_feedback") or {}
    pending_matches = pending.get("match_id") == str(match_doc["_id"]) and pending.get("other_id") == req.other_id
    pending_date = user_doc.get("pending_date_coordination") or {}
    date_matches = (
        pending_date.get("match_id") == str(match_doc["_id"])
        and pending_date.get("other_id") == req.other_id
    )

    if is_date_cancellation(req.message) and (date_matches or "協調約會" in req.message):
        role = participant_role(match_doc, req.user_id)
        date_state = match_doc.get("date_coordination", {}) or {}
        other_role = participant_role(match_doc, req.other_id)
        other_had_started = bool((date_state.get("participants", {}) or {}).get(other_role))
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$unset": {"pending_date_coordination": ""}}
        )
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {
                "$set": {
                    "date_coordination.status": "cancelled",
                    "date_coordination.cancelled_by": req.user_id,
                    "date_coordination.cancelled_at": time.time(),
                },
                "$unset": {f"date_coordination.participants.{role}": ""},
            },
        )
        if other_had_started:
            queue_mediator_event(
                req.other_id, "這次約會協調先暫停，不用再填資料。",
                "date_coordination_cancelled", match_id=str(match_doc["_id"]),
                other_id=req.user_id,
            )
        reply = "好，這次先停，我不會再追問。"
        save_private_mediator_reply(room_id, reply, "date_coordination_cancelled")
        return {"reply": reply, "pending_step": None}

    if "協調約會" in req.message and not date_matches:
        pending_date = {
            "match_id": str(match_doc["_id"]), "other_id": req.other_id,
            "stage": "availability", "data": {}
        }
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
        )
        reply = "可以。你方便平日晚上、週末早上、週末下午，還是週末晚上？我只會帶回交集。"
        save_private_mediator_reply(room_id, reply, "date_coordination_request")
        return {"reply": reply, "pending_step": "date_availability"}

    if date_matches:
        stage = pending_date.get("stage", "availability")
        data = pending_date.get("data", {})
        data[stage] = normalize_date_answer(stage, req.message)
        if stage == "availability":
            pending_date.update({"stage": "activity", "data": data})
            profiles_coll.update_one(
                {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
            )
            reply = "活動想選咖啡、散步、電影、吃飯，還是展覽？"
            save_private_mediator_reply(room_id, reply, "date_coordination_request")
            return {"reply": reply, "pending_step": "date_activity"}
        if stage == "activity":
            pending_date.update({"stage": "budget", "data": data})
            profiles_coll.update_one(
                {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
            )
            reply = "最後一題：預算抓 500 內、500 到 1000，還是 1000 以上？"
            save_private_mediator_reply(room_id, reply, "date_coordination_request")
            return {"reply": reply, "pending_step": "date_budget"}

        role = participant_role(match_doc, req.user_id)
        data["budget"] = data.pop("budget", normalize_date_answer("budget", req.message))
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {f"date_coordination.participants.{role}": data}}
        )
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$unset": {"pending_date_coordination": ""}}
        )
        refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or {}
        participants = ((refreshed.get("date_coordination") or {}).get("participants") or {})
        other_role = participant_role(match_doc, req.other_id)
        if not participants.get(other_role):
            queue_mediator_event(
                req.other_id, "對方想約你出去，我來幫你們只對交集。先跟我說你方便的時段吧。",
                "date_coordination_request", match_id=str(match_doc["_id"]), other_id=req.user_id
            )
            reply = "好，我先收著；等他也填完，我只把你們重疊的選項帶回來。"
        else:
            overlap = date_overlap(data, participants[other_role])
            if overlap["time"] and overlap["activity"]:
                result_text = f"有交集：{overlap['time']}去{overlap['activity']}，預算建議「{overlap['budget']}」。要成行再由你們自己確認。"
            else:
                result_text = "目前時間或活動還沒交集，我先不硬湊；你們可以再各給一個備選。"
            matches_coll.update_one(
                {"_id": match_doc["_id"]},
                {"$set": {"date_coordination.status": "completed",
                          "date_coordination.overlap": overlap,
                          "date_coordination.completed_at": time.time()}}
            )
            queue_mediator_event(
                req.other_id, result_text, "date_coordination_result",
                match_id=str(match_doc["_id"]), other_id=req.user_id
            )
            reply = result_text
        save_private_mediator_reply(room_id, reply, "date_coordination_result")
        return {"reply": reply, "pending_step": None}

    is_probe_command = any(phrase in req.message for phrase in ("喜歡我", "有好感", "怎麼看我", "對我的感覺", "探口風", "口風", "探探", "幫我問"))
    if pending_matches and pending.get("stage") == "probe_answer":
        kind = pending.get("kind", "fun_fact")
        requester_id = pending.get("requester_id")
        # 低敏感問題已在提問時告知會帶回大方向；直接做長度限制，避免為了摘要再等一次模型。
        safe_summary = re.sub(r"\s+", " ", req.message).strip()[:60]
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {
                participant_probe_field(match_doc, req.user_id) + ".status": "completed",
                participant_probe_field(match_doc, req.user_id) + ".completed_at": time.time(),
                participant_probe_field(match_doc, req.user_id) + ".shared_summary": safe_summary,
            }, "$push": {"probe_history": {
                "kind": kind, "asked_to": req.user_id, "answered_at": time.time(),
                "status": "completed", "shared_summary": safe_summary
            }}}
        )
        profiles_coll.update_one({"user_id": req.user_id}, {"$unset": {"pending_private_feedback": ""}})
        if requester_id and requester_id == req.other_id:
            queue_mediator_event(
                requester_id, f"我問到一個小線索：{safe_summary}",
                "probe_result", match_id=str(match_doc["_id"]), other_id=req.user_id,
                probe_kind=kind
            )
        reply = "收到，我只帶回這個生活方向，不會搬你的原話。"
        save_private_mediator_reply(room_id, reply)
        return {"reply": reply, "pending_step": None, "probe_kind": kind}

    if pending_matches and pending.get("stage") == "sentiment" and is_probe_command:
        probe_result = request_relationship_probe(req.user_id, req.other_id, False, "sentiment")
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
        requested_kind = "fun_fact" if any(word in req.message for word in ("有趣", "小事", "話題", "喜好")) else "sentiment"
        probe_result = request_relationship_probe(req.user_id, req.other_id, False, requested_kind)
        reply = probe_result["reply"]
    else:
        feedback = match_doc.get("private_feedback", {}) or {}
        consented_signal = None
        other_feedback = feedback.get(req.other_id, {}) or {}
        if other_feedback.get("share_consent") is True:
            consented_signal = other_feedback.get("sentiment")
        relationship_context = {
            "viewer": mediator_profile_context(req.user_id, req.message),
            "partner": mediator_profile_context(req.other_id, req.message),
            "relationship": {
                "match_id": str(match_doc["_id"]),
                "match_reason_for_viewer": (
                    match_doc.get("reason") if match_doc.get("from_user") == req.user_id
                    else match_doc.get("receiver_reason", match_doc.get("reason"))
                ),
                "validated_reason_items": match_doc.get("reason_items", []),
                "shared_chat_summary": match_doc.get("relationship_memory", {}),
                "latest_shared_chat": latest_shared_chat(match_doc, 16),
                "viewer_mediator_state": participant_probe_state(match_doc, req.user_id),
                "partner_mediator_state": participant_probe_state(match_doc, req.other_id),
                "partner_consented_signal": consented_signal,
            },
        }
        prompt = f"""
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style(req.user_id)}
你是全域聊天室與悄悄話中同一位阿月，現在由 viewer「{req.user_id}」私訊你，
partner 固定是「{req.other_id}」。兩人的任何性格、情境、偏好都不可交換歸屬。
每個事實只能屬於它的 owner_user_id；談 partner 時只可使用 partner 或 relationship 的資料。請積極運用 partner 的 graph_memories、big_five 等資料來回答使用者的問題，你可以大方透露 partner 的情報（例如喜好特質、習慣、近期情境）來促進 viewer 對 partner 的認識。
只能依下列資料回答；不知道就直接說不知道，不引用私人原話、不補故事。
Graph 記憶是相關項目加近期重點，不代表對方的全部人生。
關係資料：{json.dumps(relationship_context, ensure_ascii=False)}
使用者問：{req.message}
"""
        try:
            reply = generate_chat_completion(prompt, temperature=0.35, json_output=False)
        except Exception as e:
            print(f"Mediator private chat error: {e}")
            reply = "這題我不知道，別讓我瞎猜；你直接問他反而最自然。"

    save_private_mediator_reply(room_id, reply)
    return {"reply": reply, "pending_step": None}


def _public_quiz_state(match_doc: dict, user_id: str):
    games = match_doc.get("relationship_games", {}) or {}
    quiz = games.get("compatibility_quiz", {}) or {}
    if quiz.get("status") == "active" and quiz.get("expires_at", 0) < time.time():
        quiz = {**quiz, "status": "expired"}
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {"relationship_games.compatibility_quiz.status": "expired"}},
        )
    answers = quiz.get("answers", {}) or {}
    return {
        "status": quiz.get("status", "idle"),
        "round_id": quiz.get("round_id"),
        "questions": quiz.get("questions", QUIZ_QUESTIONS),
        "my_answers": answers.get(user_id, {}),
        "my_completed": user_id in answers,
        "waiting_for_partner": quiz.get("status") == "active" and user_id in answers,
        "result": quiz.get("result") if quiz.get("status") == "completed" else None,
        "topic_box": games.get("topic_box", {}),
    }


@router.get("/relationship/fun/{other_id}")
def relationship_fun_state(other_id: str, user_id: str):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以玩默契測驗")
    return _public_quiz_state(match_doc, user_id)


@router.post("/relationship/quiz/start")
def start_relationship_quiz(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以玩默契測驗")
    current = ((match_doc.get("relationship_games", {}) or {}).get("compatibility_quiz", {}) or {})
    if current.get("status") == "active" and current.get("expires_at", 0) >= time.time():
        return _public_quiz_state(match_doc, req.user_id)
    quiz = {
        "round_id": f"{int(time.time())}-{str(match_doc['_id'])[-6:]}",
        "status": "active",
        "started_by": req.user_id,
        "started_at": time.time(),
        "expires_at": time.time() + QUIZ_TTL_SECONDS,
        "questions": QUIZ_QUESTIONS,
        "answers": {},
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.compatibility_quiz": quiz}},
    )
    queue_mediator_event(
        req.other_id, f"{req.user_id} 邀你玩三題默契小測驗，答案不同也不會被公開。",
        "compatibility_quiz_invite", match_id=str(match_doc["_id"]), other_id=req.user_id,
    )
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    return _public_quiz_state(refreshed, req.user_id)


@router.post("/relationship/quiz/answer")
def answer_relationship_quiz(req: RelationshipQuizAnswerRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以玩默契測驗")
    quiz = ((match_doc.get("relationship_games", {}) or {}).get("compatibility_quiz", {}) or {})
    if quiz.get("status") != "active" or quiz.get("expires_at", 0) < time.time():
        raise HTTPException(status_code=409, detail="這一輪測驗已結束，請重新開始")
    valid_answers = {}
    for question in quiz.get("questions", QUIZ_QUESTIONS):
        answer = req.answers.get(question["id"])
        if answer not in question["options"]:
            raise HTTPException(status_code=422, detail=f"請完成「{question['text']}」")
        valid_answers[question["id"]] = answer
    answers = dict(quiz.get("answers", {}) or {})
    answers[req.user_id] = valid_answers
    quiz["answers"] = answers

    participants = {match_doc["from_user"], match_doc["to_user"]}
    if participants.issubset(answers.keys()):
        first, second = match_doc["from_user"], match_doc["to_user"]
        matches = []
        for question in quiz.get("questions", QUIZ_QUESTIONS):
            question_id = question["id"]
            if answers[first][question_id] == answers[second][question_id]:
                matches.append({
                    "question_id": question_id,
                    "question": question["text"],
                    "answer": answers[first][question_id],
                })
        quiz["status"] = "completed"
        quiz["completed_at"] = time.time()
        quiz["result"] = {"match_count": len(matches), "matches": matches, "total": len(QUIZ_QUESTIONS)}
        summary = (
            f"你們三題中了 {len(matches)} 題："
            + ("、".join(item["answer"] for item in matches) if matches else "答案不太一樣，也正好有話題")
        )
        save_message(
            generate_room_id(first, second), "ai_assistant", summary,
            message_type="mediator_card",
            metadata={"event_type": "compatibility_quiz_result", "result": quiz["result"]},
        )
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.compatibility_quiz": quiz}},
    )
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    return _public_quiz_state(refreshed, req.user_id)


@router.post("/relationship/quiz/cancel")
def cancel_relationship_quiz(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以操作")
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "relationship_games.compatibility_quiz.status": "cancelled",
            "relationship_games.compatibility_quiz.cancelled_by": req.user_id,
            "relationship_games.compatibility_quiz.cancelled_at": time.time(),
        }},
    )
    return {"status": "cancelled"}


@router.post("/relationship/topic")
def draw_relationship_topic(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只有已配對的聊天室可以抽話題")
    games = match_doc.get("relationship_games", {}) or {}
    quiz = games.get("compatibility_quiz", {}) or {}
    if quiz.get("status") != "completed":
        raise HTTPException(status_code=409, detail="先一起完成默契測驗，才會解鎖話題盲盒")
    today = time.strftime("%Y-%m-%d", time.localtime())
    topic_box = games.get("topic_box", {}) or {}
    if topic_box.get("drawn_date") == today:
        return {"status": "already_drawn", "topic": topic_box.get("topic")}
    overlaps = (quiz.get("result", {}) or {}).get("matches", [])
    if overlaps:
        common = overlaps[0]["answer"]
        topic = f"你們都選了「{common}」：如果現在就成行，你最想怎麼安排？"
        source = "quiz_overlap"
    else:
        reason_items = match_doc.get("reason_items", []) or []
        if reason_items:
            topic = f"阿月想聽你們聊聊：{reason_items[0].get('text')}，你們自己覺得準嗎？"
            source = "validated_match_reason"
        else:
            topic = "今天的盲盒題：最近有哪件小事，讓你覺得普通的一天突然變好了？"
            source = "safe_question_bank"
    topic_box = {
        "drawn_date": today, "drawn_at": time.time(), "drawn_by": req.user_id,
        "topic": topic, "source": source,
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.topic_box": topic_box}},
    )
    save_message(
        generate_room_id(match_doc["from_user"], match_doc["to_user"]),
        "ai_assistant", topic, message_type="mediator_card",
        metadata={"event_type": "topic_box", "source": source},
    )
    return {"status": "drawn", "topic": topic}


@router.get("/proactive_check")
def proactive_check(user_id: str, conversation_active: bool = False):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}

    queue_due_feedback(user_id)

    notice_doc = profiles_coll.find_one_and_update(
        {"user_id": user_id, "memory_notices.0": {"$exists": True}},
        {"$pop": {"memory_notices": -1}},
        projection={"memory_notices": 1},
        return_document=ReturnDocument.BEFORE
    )
    if notice_doc and notice_doc.get("memory_notices"):
        notice = notice_doc["memory_notices"][0]
        return {
            "has_new": True, "surface": "ephemeral_notice", "type": "memory_learned",
            "message": notice.get("message"), "memory": notice.get("memory")
        }

    # Deliver the highest-priority queued event. Claiming by event_id prevents
    # duplicate delivery when the same account is open in multiple tabs.
    event = claim_next_mediator_event(user_id)
    if event:
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
            "event_id": event.get("event_id"),
            "event_type": event_type,
            "match_id": event.get("match_id"),
            "other_id": other_id,
            "probe_id": event.get("probe_id"),
            "proposal_role": event.get("proposal_role"),
            "matches": event.get("matches", []),
            "actions": event.get("actions", [])
        }
        if relationship_private:
            room_id = generate_mediator_private_room_id(user_id, other_id)
            if event_type in {"feedback_request", "probe_question"}:
                # Older builds could enqueue the same probe on every poll. Once
                # one is claimed, discard the remaining copies for this relation.
                profiles_coll.update_one(
                    {"user_id": user_id},
                    {"$pull": {"mediator_inbox": {
                        "type": {"$in": ["feedback_request", "probe_question"]},
                        "match_id": event.get("match_id"),
                    }}},
                )
                state = participant_probe_state(event_match, user_id)
                if event.get("probe_id") and state.get("probe_id") != event.get("probe_id"):
                    return {"has_new": False, "deduplicated": True}
                asked_at = float(state.get("asked_at", 0))
                duplicate_query = {
                    "room_id": room_id,
                    "metadata.event_type": event_type,
                }
                if event.get("probe_id"):
                    duplicate_query["metadata.probe_id"] = event.get("probe_id")
                else:
                    duplicate_query["timestamp"] = {"$gte": asked_at - 1}
                duplicate = asked_at and messages_coll.find_one(duplicate_query)
                if duplicate and state.get("status") in {"awaiting_answer", "awaiting_sentiment", "awaiting_consent"}:
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
            if event_type in {"feedback_request", "probe_question"}:
                requester_id = event.get("requester_id") or participant_probe_state(event_match, user_id).get("requester_id")
                probe_kind = event.get("probe_kind") or participant_probe_state(event_match, user_id).get("kind", "sentiment")
                stage = "sentiment" if probe_kind == "sentiment" else "probe_answer"
                profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_private_feedback": {
                    "match_id": str(event_match["_id"]), "other_id": other_id,
                    "stage": stage, "kind": probe_kind, "origin": event.get("origin", "auto"),
                    "requester_id": requester_id, "probe_id": event.get("probe_id")}}})
                matches_coll.update_one({"_id": event_match["_id"]}, {"$set": {
                    participant_probe_field(event_match, user_id) + ".status":
                        "awaiting_sentiment" if stage == "sentiment" else "awaiting_answer",
                    participant_probe_field(event_match, user_id) + ".asked_at": time.time()}})
            elif event_type == "date_coordination_request":
                profiles_coll.update_one({"user_id": user_id}, {"$set": {
                    "pending_date_coordination": {
                        "match_id": str(event_match["_id"]), "other_id": other_id,
                        "stage": "availability", "data": {}
                    }
                }})
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

    last_activity = float(user_doc.get("last_user_activity_at", 0) or 0)
    handled_activity = float(user_doc.get("last_followup_activity_at", 0) or 0)
    current_time = time.time()

    # 主動關心改為「最後互動後延遲」：沒有新互動、仍在輸入或尚未到期都不插話。
    if (
        last_activity > handled_activity
        and not conversation_active
        and current_time - last_activity >= freq_seconds
    ):
        
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
            profiles_coll.update_one({"user_id": user_id}, {"$set": {
                "ai_chat_locked": False,
                "ai_chat_interaction_count": 0,
                "last_proactive_time": current_time,
                "last_followup_activity_at": last_activity,
            }})
            return {"has_new": True, "message": ai_reply}
        except Exception as e:
            print(f"Proactive chat error: {e}")
            return {"has_new": False}
            
    return {"has_new": False}
