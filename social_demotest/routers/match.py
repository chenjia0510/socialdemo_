import time
import requests
import json
from fastapi import APIRouter, HTTPException, BackgroundTasks
from models import MatchRequest, AcceptRequest
from database import profiles_coll, matches_coll
from services.ai_service import get_embedding, generate_peer_first_message
from services.chat_service import generate_room_id, save_message
from bson.objectid import ObjectId

router = APIRouter(prefix="/api/match", tags=["Match"])

def strip_agent_payload(doc):
    """Return a copy safe to send to the LLM agent without large vector fields."""
    if not isinstance(doc, dict):
        return doc
    clean = dict(doc)
    clean.pop("context_embedding", None)
    return clean

def generate_matches_for_user(user_id: str):
    """Run the existing matching pipeline for either a manual or proactive request."""
    req = MatchRequest(user_id=user_id)
    total_start = time.perf_counter()
    print(f"[TIMING][V1 /api/match] start user={req.user_id}")

    step_start = time.perf_counter()
    user_doc = profiles_coll.find_one({"user_id": req.user_id}, {"_id": 0})
    print(f"[TIMING][V1 /api/match] load user profile: {time.perf_counter() - step_start:.3f}s")
    if not user_doc:
         raise HTTPException(status_code=400, detail="User context not found.")
         
    user_embedding = user_doc.get("context_embedding", [])
    if not user_embedding:
        step_start = time.perf_counter()
        ctx = user_doc.get("current_context", "交朋友")
        user_embedding = get_embedding(ctx)
        user_doc["current_context"] = ctx
        profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"context_embedding": user_embedding, "current_context": ctx}})
        print(f"[TIMING][V1 /api/match] create missing embedding: {time.perf_counter() - step_start:.3f}s")
    
    step_start = time.perf_counter()
    existing_matches = list(matches_coll.find({"$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]}))
    excluded_users = {req.user_id}
    current_revision = int(user_doc.get("current_context_revision", 0))
    for m in existing_matches:
        status = m.get("status")
        age = time.time() - float(m.get("created_at", 0))
        should_exclude = status in {"accepted", "draft", "pending"}
        if status == "declined":
            should_exclude = age < 30 * 86400 or int(m.get("context_revision", 0)) == current_revision
        if should_exclude:
            excluded_users.add(m["from_user"])
            excluded_users.add(m["to_user"])
    print(f"[TIMING][V1 /api/match] load existing matches: {time.perf_counter() - step_start:.3f}s count={len(existing_matches)}")
    
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "context_embedding",
                "queryVector": user_embedding,
                "numCandidates": 50,
                "limit": 20
            }
        },
        {
            "$match": {
                "user_id": { "$nin": list(excluded_users) }
            }
        },
        {
            "$addFields": {
                "score": { "$meta": "vectorSearchScore" }
            }
        },
        {
            "$limit": 5
        },
        {
            "$project": {
                "_id": 0
            }
        }
    ]
    
    try:
        step_start = time.perf_counter()
        raw_candidates = list(profiles_coll.aggregate(pipeline))
        print(f"[TIMING][V1 /api/match] Mongo vector search: {time.perf_counter() - step_start:.3f}s raw_candidates={len(raw_candidates)}")
    except Exception as e:
        print(f"[TIMING][V1 /api/match] Mongo vector search failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Vector search failed: {e}")
        raise HTTPException(status_code=500, detail="Vector search failed. 請確認已在 MongoDB Atlas 建立 vector_index 且具備 context_embedding 欄位。")

    top_5_candidates = []
    for c in raw_candidates:
        score = c.get("score", 0.0)
        top_5_candidates.append((score, c))
    
    if not top_5_candidates:
        raise HTTPException(status_code=404, detail="Not enough candidates.")

    # 將配對決策委派給 9001 港口的 V2 Agent
    clean_candidates = [c[1] if isinstance(c, tuple) else c for c in top_5_candidates]
    
    # 取得 target_user 的 deep_profile
    target_deep_profile = user_doc.get("deep_profile", {})
    
    # 取得每位 candidate 的 deep_profile
    step_start = time.perf_counter()
    for c in clean_candidates:
        c_doc = profiles_coll.find_one({"user_id": c.get("user_id")}, {"deep_profile": 1, "_id": 0})
        if c_doc and c_doc.get("deep_profile"):
            c["deep_profile"] = c_doc["deep_profile"]
    print(f"[TIMING][V1 /api/match] hydrate candidate deep_profile: {time.perf_counter() - step_start:.3f}s candidates={len(clean_candidates)}")
    
    agent_user_doc = strip_agent_payload(user_doc)
    agent_candidates = [strip_agent_payload(c) for c in clean_candidates]
    payload = {
        "target_user": agent_user_doc,
        "candidates": agent_candidates,
        "target_deep_profile": target_deep_profile
    }
    try:
        original_payload_chars = len(json.dumps({
            "target_user": user_doc,
            "candidates": clean_candidates,
            "target_deep_profile": target_deep_profile
        }, ensure_ascii=False, default=str))
        stripped_payload_chars = len(json.dumps(payload, ensure_ascii=False, default=str))
        print(
            "[TIMING][V1 /api/match] Agent payload stripped "
            f"context_embedding original_chars={original_payload_chars} "
            f"stripped_chars={stripped_payload_chars} "
            f"saved_chars={original_payload_chars - stripped_payload_chars}"
        )
    except Exception as e:
        print(f"[TIMING][V1 /api/match] Agent payload size logging failed: {e}")
    
    try:
        print("📞 正在打電話給 9001 港口的媒婆 Agent...")
        step_start = time.perf_counter()
        agent_resp = requests.post("http://127.0.0.1:9001/api/match", json=payload, timeout=120)
        print(f"[TIMING][V1 /api/match] 9001 Agent HTTP roundtrip: {time.perf_counter() - step_start:.3f}s status={agent_resp.status_code}")
        agent_resp.raise_for_status()
        
        step_start = time.perf_counter()
        agent_data = agent_resp.json()
        print(f"[TIMING][V1 /api/match] parse Agent response JSON: {time.perf_counter() - step_start:.3f}s")
        # 🥚 雙黃蛋：解析 matches 陣列
        agent_matches = agent_data.get("matches", [])
        if not agent_matches:
            raise HTTPException(status_code=500, detail="Agent 未回傳任何配對結果")
        print(f"✅ Agent 回應: {len(agent_matches)} 位候選人")
    except requests.RequestException as e:
        print(f"❌ 無法連線到 9001 Agent: {e}")
        raise HTTPException(status_code=503, detail=f"配對 Agent (port 9001) 無法連線: {e}")
    
    # 阿月一次只牽一條線，避免同時丟出候選人清單。
    step_start = time.perf_counter()
    result_matches = []
    for m in agent_matches[:1]:
        matched_id = m.get("matched_user_id")
        reason = m.get("recommendation_reason", "")
        receiver_reason = m.get("receiver_reason", "")
        contrast_label = m.get("contrast_label", "")
        distinctive_tags = m.get("distinctive_tags", [])
        
        if not matched_id:
            continue
        
        match_doc = {
            "from_user": req.user_id,
            "to_user": matched_id,
            "reason": reason,
            "receiver_reason": receiver_reason,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "status": "draft",
            "delivery_channel": "mediator_chat",
            "context_revision": int(user_doc.get("current_context_revision", 0)),
            "created_at": time.time()
        }
        insert_result = matches_coll.insert_one(match_doc)
        
        # 查詢候選人的 profile 供前端渲染
        to_doc = profiles_coll.find_one({"user_id": matched_id}, {"_id": 0})
        
        result_matches.append({
            "match_id": str(insert_result.inserted_id),
            "matched_user_id": matched_id,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "recommendation_reason": reason,
            "receiver_reason": receiver_reason,
            "big_five": to_doc.get("big_five", {}) if to_doc else {},
            "current_context": to_doc.get("current_context", "") if to_doc else ""
        })
        print(f"  ✅ 建立 draft 配對: {req.user_id} → {matched_id} [{contrast_label}]")
    
    print(f"[TIMING][V1 /api/match] persist draft matches and load result profiles: {time.perf_counter() - step_start:.3f}s result_matches={len(result_matches)}")

    debug_candidates = []
    for score, doc in top_5_candidates:
        debug_candidates.append({
            "user_id": doc.get("user_id"),
            "score": round(score * 100, 2),
            "context": doc.get("current_context"),
            "big_five_summary": doc.get("big_five", {}).get("summary", "")
        })
    
    print(f"[TIMING][V1 /api/match] total: {time.perf_counter() - total_start:.3f}s user={req.user_id}")
    return {
        "status": "success",
        "matches": result_matches,
        "debug_info": debug_candidates
    }

def _queue_match_event(user_id: str, event_type: str, message: str, **extra):
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$push": {"mediator_inbox": {"type": event_type, "message": message, "created_at": time.time(), **extra}}},
        upsert=True
    )


def create_proactive_match_proposal(user_id: str, source: str = "automatic", force_new: bool = False):
    """Create one proposal and always surface success or failure to Ayue's chat."""
    now = time.time()
    active = matches_coll.find_one({
        "status": {"$in": ["draft", "pending"]},
        "$or": [{"from_user": user_id}, {"to_user": user_id}]
    })
    if active:
        _queue_match_event(user_id, "match_search_blocked", "手上這條線還沒確認完，先把它處理好，我再幫你看下一位。")
        return {"status": "already_active"}
    claimed = profiles_coll.find_one_and_update(
        {"user_id": user_id,
         "$or": [{"matchmaking_in_progress": {"$ne": True}}, {"matchmaking_started_at": {"$lt": now - 300}}],
         "active_match_proposal_id": {"$exists": False}},
        {"$set": {"matchmaking_in_progress": True, "matchmaking_started_at": now,
                  "match_search": {"status": "searching", "source": source, "started_at": now}}},
        return_document=True
    )
    if not claimed:
        return {"status": "already_searching"}
    try:
        result = generate_matches_for_user(user_id)
        suggestions = result.get("matches", [])[:1]
        if not suggestions:
            raise HTTPException(status_code=404, detail="目前沒有新的候選人")
        first = suggestions[0]
        candidate_id = first.get("matched_user_id", "這個人")
        reason = first.get("recommendation_reason", "").strip()
        tone = (profiles_coll.find_one({"user_id": user_id}, {"mediator_tone": 1}) or {}).get("mediator_tone", "friend")
        opening = {"friend": "欸，我今天一看到他就想到你。", "gentle": "我今天看到他時，第一個就想到你。",
                   "enthusiastic": "阿月的媒人雷達響了，我一看到他就想到你！"}.get(tone, "欸，我今天一看到他就想到你。")
        event = {"type": "match_proposal", "message": f"{opening}{reason} 有興趣的話，我幫你問問。",
                 "matches": suggestions, "match_id": first.get("match_id"), "other_id": candidate_id,
                 "proposal_role": "initiator", "debug_info": result.get("debug_info", []), "created_at": time.time()}
        current = profiles_coll.find_one({"user_id": user_id}, {"current_context_revision": 1}) or {}
        profiles_coll.update_one({"user_id": user_id}, {"$push": {"mediator_inbox": event}, "$set": {
            "last_auto_match_revision": current.get("current_context_revision", 0),
            "active_match_proposal_id": first.get("match_id"),
            "match_search": {"status": "found", "source": source, "other_id": candidate_id, "completed_at": time.time()}}})
        return {"status": "queued", "other_id": candidate_id}
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        no_candidates = "candidate" in detail.lower() or "候選" in detail or getattr(exc, "status_code", 0) == 404
        message = "這輪我暫時沒看到適合的新對象，等資料多一點我再幫你看。" if no_candidates else "我剛剛找人的路上卡了一下，沒有假裝成功；晚點可以再叫我試一次。"
        _queue_match_event(user_id, "match_search_empty" if no_candidates else "match_search_failed", message, error=detail[:200])
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"match_search": {
            "status": "no_candidates" if no_candidates else "failed", "source": source,
            "error": detail[:200], "completed_at": time.time()}}})
        print(f"Proactive matchmaking failed for {user_id}: {exc}")
        return {"status": "no_candidates" if no_candidates else "failed", "detail": detail}
    finally:
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"matchmaking_in_progress": False}, "$unset": {"matchmaking_started_at": ""}})

@router.post("/request")
def request_next_match(req: MatchRequest, background_tasks: BackgroundTasks):
    active = matches_coll.find_one({"status": {"$in": ["draft", "pending"]},
        "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]})
    if active:
        return {"status": "already_active"}
    profile = profiles_coll.find_one({"user_id": req.user_id}) or {}
    if profile.get("matchmaking_in_progress") and profile.get("matchmaking_started_at", 0) > time.time() - 300:
        return {"status": "already_searching"}
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
        "status": "queued", "source": req.source, "requested_at": time.time()}}}, upsert=True)
    background_tasks.add_task(create_proactive_match_proposal, req.user_id, req.source, req.force_new)
    return {"status": "queued"}

@router.get("/status")
def get_match_status(user_id: str):
    profile = profiles_coll.find_one({"user_id": user_id}, {"match_search": 1, "matchmaking_in_progress": 1}) or {}
    return {"match_search": profile.get("match_search", {"status": "idle"}),
            "matchmaking_in_progress": bool(profile.get("matchmaking_in_progress"))}

@router.post("")
def match_endpoint(req: MatchRequest):
    return generate_matches_for_user(req.user_id)

@router.post("/accept")
def accept_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    
    current_status = match_doc.get("status")
    from_id = match_doc["from_user"]
    to_id = match_doc["to_user"]
    reason = match_doc.get("reason", "")
    
    # 🔄 狀態機：雙情境路由
    if current_status == "draft" and req.user_id == from_id:
        matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "pending"}})
        profiles_coll.update_one(
            {"user_id": from_id},
            {"$unset": {"active_match_proposal_id": ""}, "$set": {"match_search.status": "idle"}}
        )
        receiver_reason = match_doc.get("receiver_reason") or reason
        incoming_message = (
            f"我剛幫你探到一個人：{from_id}。{receiver_reason} "
            "我沒有替你答應，先來問本人——你有興趣認識看看嗎？"
        )
        profiles_coll.update_one(
            {"user_id": to_id},
            {"$push": {"mediator_inbox": {
                "type": "incoming_match_interest",
                "message": incoming_message,
                "match_id": req.match_id,
                "other_id": from_id,
                "proposal_role": "receiver",
                "matches": [{
                    "match_id": req.match_id,
                    "matched_user_id": from_id,
                    "contrast_label": match_doc.get("contrast_label", ""),
                    "distinctive_tags": match_doc.get("distinctive_tags", []),
                    "recommendation_reason": receiver_reason
                }],
                "created_at": time.time()
            }}},
            upsert=True
        )
        print(f"📤 發起者 {from_id} 確認邀請 {to_id}：draft → pending")
        return {"status": "success", "new_status": "pending"}
    
    elif current_status == "pending" and req.user_id == to_id:
        # 情境 B：接收者互相接受 → pending → accepted
        matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "accepted"}})
        profiles_coll.update_many(
            {"user_id": {"$in": [from_id, to_id]}},
            {"$unset": {"active_match_proposal_id": ""}, "$set": {"match_search.status": "idle"}}
        )
        print(f"🤝 接收者 {to_id} 接受邀請 {from_id}：pending → accepted")
        
        # ✅ 觸發 AI 破冰訊息
        initiator_doc = profiles_coll.find_one({"user_id": from_id})
        target_doc = profiles_coll.find_one({"user_id": to_id})
        
        def send_first_msg():
            first_msg = generate_peer_first_message(initiator_doc, target_doc, reason)
            room_id = generate_room_id(from_id, to_id)
            save_message(room_id, from_id, first_msg)
            
        background_tasks.add_task(send_first_msg)
        
        # ✅ 觸發全域抽象化反思（配對成功 → 歸納通用法則）
        from_big_five = initiator_doc.get("big_five", {}) if initiator_doc else {}
        from_context = initiator_doc.get("current_context", "") if initiator_doc else ""
        to_big_five = target_doc.get("big_five", {}) if target_doc else {}
        to_context = target_doc.get("current_context", "") if target_doc else ""
        
        def trigger_global_reflection():
            try:
                requests.post("http://127.0.0.1:9001/api/global_reflection", json={
                    "from_big_five": from_big_five,
                    "from_context": from_context,
                    "to_big_five": to_big_five,
                    "to_context": to_context
                }, timeout=30)
                print("🧠 已觸發全域抽象化反思")
            except Exception as e:
                print(f"⚠️ 觸發全域反思失敗: {e}")
        
        background_tasks.add_task(trigger_global_reflection)

        for user_id, other_id in ((from_id, to_id), (to_id, from_id)):
            profiles_coll.update_one(
                {"user_id": user_id},
                {"$push": {"mediator_inbox": {
                    "type": "match_connected",
                    "message": f"好，{other_id} 也點頭了！聊天室已經替你們開好。先自然打聲招呼，別一上來就像面試官，我會在旁邊幫你顧氣氛。",
                    "match_id": req.match_id,
                    "other_id": other_id,
                    "created_at": time.time()
                }}}
            )
        
        return {"status": "success", "new_status": "accepted"}
    
    else:
        # 無效的狀態轉換
        raise HTTPException(
            status_code=400, 
            detail=f"無效的狀態轉換：目前狀態={current_status}，操作者={req.user_id}（發起者={from_id}，接收者={to_id}）"
        )

@router.post("/decline")
def decline_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    print(f"📥 V1 收到婉拒請求，準備轉發給 Agent: {req.explicit_reasons}")
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    
    current_status = match_doc.get("status")
    from_id = match_doc["from_user"]
    to_id = match_doc["to_user"]
    
    matches_coll.update_one({"_id": ObjectId(req.match_id)}, {"$set": {"status": "declined"}})
    profiles_coll.update_many(
        {"user_id": {"$in": [from_id, to_id]}},
        {"$unset": {"active_match_proposal_id": ""}, "$set": {"match_search.status": "idle"}}
    )
    
    # 🔄 狀態機：雙情境路由回饋
    if current_status == "draft" and req.user_id == from_id:
        # 情境 A：發起者婉拒草稿 → 回饋「發起者」的偏好
        to_doc = profiles_coll.find_one({"user_id": to_id})
        target_traits = to_doc.get("big_five", {}) if to_doc else {}
        
        def notify_agent_decline_initiator():
            try:
                feedback_payload = {
                    "user_id": from_id,       # 發起者
                    "target_id": to_id,       # 被婉拒的候選人
                    "action": "decline",
                    "target_traits": target_traits,
                    "explicit_reasons": req.explicit_reasons
                }
                print(f"📝 發起者婉拒草稿回饋: {feedback_payload}")
                resp = requests.post("http://127.0.0.1:9001/api/feedback", json=feedback_payload, timeout=30)
                resp.raise_for_status()
                print("📝 已通知 Agent 發起者婉拒回饋")
            except Exception as e:
                print(f"❌ 轉發 Feedback 給 Agent 失敗: {e}")
                print(f"⚠️ 通知 Agent 回饋失敗: {e}")
        
        background_tasks.add_task(notify_agent_decline_initiator)
        print(f"❌ 發起者 {from_id} 婉拒草稿 {to_id}：draft → declined")
        return {"status": "success", "new_status": "declined", "context": "initiator_declined_draft"}
    
    elif current_status == "pending" and req.user_id == to_id:
        # 情境 B：接收者婉拒邀請 → 回饋「接收者」的偏好
        from_doc = profiles_coll.find_one({"user_id": from_id})
        target_traits = from_doc.get("big_five", {}) if from_doc else {}
        
        def notify_agent_decline_receiver():
            try:
                feedback_payload = {
                    "user_id": to_id,         # 接收者
                    "target_id": from_id,     # 被婉拒的發起者
                    "action": "decline",
                    "target_traits": target_traits,
                    "explicit_reasons": req.explicit_reasons
                }
                print(f"📝 接收者婉拒邀請回饋: {feedback_payload}")
                resp = requests.post("http://127.0.0.1:9001/api/feedback", json=feedback_payload, timeout=10)
                resp.raise_for_status()
                print("📝 已通知 Agent 接收者婉拒回饋")
            except Exception as e:
                print(f"❌ 轉發 Feedback 給 Agent 失敗: {e}")
                print(f"⚠️ 通知 Agent 回饋失敗: {e}")
        
        background_tasks.add_task(notify_agent_decline_receiver)
        print(f"❌ 接收者 {to_id} 婉拒邀請 {from_id}：pending → declined")
        return {"status": "success", "new_status": "declined", "context": "receiver_declined_pending"}
    
    else:
        # 無效的狀態轉換
        raise HTTPException(
            status_code=400,
            detail=f"無效的狀態轉換：目前狀態={current_status}，操作者={req.user_id}（發起者={from_id}，接收者={to_id}）"
        )
