import random
import requests
from fastapi import APIRouter
from models import ClearRequest, SettingsRequest, MediatorToneRequest, ProfileMemoryActionRequest
from database import profiles_coll, matches_coll, messages_coll
from services.ai_service import get_embedding

router = APIRouter(prefix="/api", tags=["System"])

@router.get("/init")
def init_system(user_id: str):
    profiles = list(profiles_coll.find({}, {"user_id": 1, "big_five": 1, "current_context": 1, "_id": 0}))
    users = ["demo_user"]
    
    is_complete = False
    my_context = "交朋友"
    my_bf_summary = "尚無性格分析資料"
    
    is_deep_complete = False
    my_deep_summary = "尚無深層價值觀分析資料"
    proactive_frequency = "normal"
    mediator_tone = "friend"
    mediator_tone_selected = False
    probe_mode = "balanced"
    profile_memories = []
    context_revision = 0
    match_search = {"status": "idle"}
    onboarding_completed = False
    
    for p in profiles:
        uid = p.get("user_id")
        if uid and uid not in users:
            users.append(uid)
        
        if uid == user_id:
            bf = p.get("big_five", {})
            my_context = p.get("current_context", "交朋友")
            if bf and len(bf) >= 5:
                is_complete = True
                my_bf_summary = bf.get("summary", "已完成性格分析，具備基本資料。")
                
    # 取得深層價值觀分析狀態
    my_dp_values = None
    my_dp_future = None
    my_deep_profile = None
    my_initial_interest = None
    my_doc = profiles_coll.find_one({"user_id": user_id})
    if my_doc:
        dp = my_doc.get("deep_profile", {})
        if dp and dp.get("summary"):
            is_deep_complete = True
            my_deep_summary = dp.get("summary", "已完成深層價值觀分析。")
            # 提供深層價值觀欄位供前端 dropdown 顯示
            core_vals = dp.get("core_values") or dp.get("values")
            my_dp_values = "、".join(core_vals) if isinstance(core_vals, list) else (core_vals or None)
            my_dp_future = dp.get("life_philosophy", None) or dp.get("ideal_future", None)
            # 回傳完整 deep_profile 物件供前端組合顯示
            my_deep_profile = dp
        proactive_frequency = my_doc.get("proactive_frequency", "normal")
        mediator_tone = my_doc.get("mediator_tone", "friend")
        mediator_tone_selected = bool(my_doc.get("mediator_tone_selected", False))
        probe_mode = my_doc.get("probe_mode", "balanced")
        profile_memories = my_doc.get("profile_memory_preview", [])
        context_revision = int(my_doc.get("current_context_revision", 0))
        match_search = my_doc.get("match_search", {"status": "idle"})
        onboarding_completed = bool(my_doc.get("onboarding_completed", False))
        my_initial_interest = my_doc.get("initial_interest", None)
                
    return {
        "users": users,
        "is_complete": is_complete,
        "my_context": my_context,
        "my_bf_summary": my_bf_summary,
        "is_deep_complete": is_deep_complete,
        "my_deep_summary": my_deep_summary,
        "my_dp_values": my_dp_values,
        "my_dp_future": my_dp_future,
        "my_deep_profile": my_deep_profile,
        "my_initial_interest": my_initial_interest,
        "proactive_frequency": proactive_frequency,
        "mediator_tone": mediator_tone,
        "mediator_tone_selected": mediator_tone_selected,
        "probe_mode": probe_mode,
        "profile_memories": profile_memories,
        "current_context_revision": context_revision,
        "match_search": match_search,
        "onboarding_completed": onboarding_completed
    }

@router.post("/seed")
def seed_data():
    hobbies = ["想去喝咖啡", "晚上想看電影", "想找人打籃球", "週末想去郊外圖書館看書", "想要去居酒屋小酌"]
    personalities = [
        {"O": 8, "C": 7, "E": 9, "A": 8, "N": 3, "summary": "開朗活潑，喜歡戶外活動，容易親近。"},
        {"O": 5, "C": 9, "E": 4, "A": 6, "N": 4, "summary": "嚴謹踏實，作風穩健，偏好安靜的環境。"},
        {"O": 9, "C": 5, "E": 7, "A": 8, "N": 5, "summary": "充滿好奇心，點子很多，喜歡嘗試新鮮事物。"},
        {"O": 6, "C": 8, "E": 3, "A": 7, "N": 6, "summary": "內斂溫和，做事有條理，是個可靠的傾聽者。"},
        {"O": 7, "C": 6, "E": 8, "A": 5, "N": 4, "summary": "直率果斷，行動力強，喜歡與人交流辯論。"}
    ]
    deep_profiles = [
        {"life_philosophy": "及時行樂，享受每個當下", "attachment_style": "安全型", "decision_style": "直覺型", "core_values": ["自由", "快樂", "冒險"], "summary": "活在當下的樂天派，重視自由與快樂"},
        {"life_philosophy": "穩紮穩打，慢工出細活", "attachment_style": "穩定型", "decision_style": "深思熟慮型", "core_values": ["可靠", "耐心", "誠實"], "summary": "重視承諾與穩定的務實主義者"},
        {"life_philosophy": "世界是一本大書，不旅行的人只讀了一頁", "attachment_style": "探索型", "decision_style": "創意型", "core_values": ["好奇", "成長", "體驗"], "summary": "充滿好奇心的探索者，永遠在尋找新視角"},
        {"life_philosophy": "傾聽是最溫柔的陪伴", "attachment_style": "照顧型", "decision_style": "謹慎型", "core_values": ["同理", "信任", "安穩"], "summary": "溫柔可靠的傾聽者，重視深層連結"},
        {"life_philosophy": "行動是治癒焦慮的最佳良藥", "attachment_style": "獨立型", "decision_style": "果斷型", "core_values": ["效率", "勇氣", "自主"], "summary": "果斷的行動派，相信做就對了"}
    ]
    fake_users = []
    profiles_coll.delete_many({"user_id": {"$regex": "^seed_user"}})
    for i in range(1, 11):
        uid = f"seed_user_{i:02d}"
        ctx = random.choice(hobbies)
        bf = random.choice(personalities)
        dp = random.choice(deep_profiles)
        fake_users.append({
            "user_id": uid, "big_five": bf,
            "deep_profile": dp,
            "current_context": ctx, "context_embedding": get_embedding(ctx)
        })
    profiles_coll.insert_many(fake_users)
    return {"status": "success", "message": "10 seed profiles created."}

@router.post("/clear")
def clear_data(req: ClearRequest):
    profiles_coll.delete_many({})
    matches_coll.delete_many({})
    messages_coll.delete_many({})
    
    # 嘗試通知 9001 埠口的 Agent 清空 Neo4j Graph
    try:
        resp = requests.post("http://127.0.0.1:9001/api/clear_graph", timeout=10)
        resp.raise_for_status()
        print("🧠 已成功通知 Agent 清空 Neo4j Graph")
    except Exception as e:
        print(f"⚠️ 通知 Agent 清空 Neo4j Graph 失敗: {e}")
        
    return {"status": "success"}

@router.get("/notifications")
def get_notifications(user_id: str):
    """查詢指定 user 的待回應配對邀請（pending）。"""
    pending = list(matches_coll.find({
        "to_user": user_id,
        "status": "pending",
        "delivery_channel": {"$ne": "mediator_chat"}
    }))
    results = []
    for p in pending:
        # 查詢發起人的完整 profile，取得 big_five 與 context 供前端渲染 Checkbox
        from_doc = profiles_coll.find_one({"user_id": p["from_user"]}, {"_id": 0})
        from_big_five = from_doc.get("big_five", {}) if from_doc else {}
        from_context = from_doc.get("current_context", "") if from_doc else ""
        from_distinctive_tags = from_doc.get("distinctive_tags", []) if from_doc else []
        results.append({
            "match_id": str(p["_id"]),
            "from_user": p["from_user"],
            "reason": p["reason"],
            "receiver_reason": p.get("receiver_reason", p.get("reason", "")),
            "from_user_big_five": from_big_five,
            "from_user_context": from_context,
            "from_user_distinctive_tags": from_distinctive_tags
        })
    return {"notifications": results}

@router.post("/settings")
def update_settings(req: SettingsRequest):
    """更新使用者設定（如主動配對頻率）"""
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"proactive_frequency": req.proactive_frequency}},
        upsert=True
    )
    return {"status": "success", "proactive_frequency": req.proactive_frequency}

@router.post("/settings/mediator")
def update_mediator_tone(req: MediatorToneRequest):
    allowed = {"friend", "gentle", "enthusiastic"}
    tone = req.mediator_tone if req.mediator_tone in allowed else "friend"
    update = {"mediator_tone": tone, "mediator_tone_selected": True}
    if req.probe_mode in {"balanced", "active", "manual"}:
        update["probe_mode"] = req.probe_mode
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": update}, upsert=True)
    return {"status": "success", "mediator_tone": tone, "probe_mode": update.get("probe_mode")}

@router.post("/onboarding/complete")
def complete_onboarding(req: ClearRequest):
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"onboarding_completed": True}},
        upsert=True
    )
    return {"status": "success", "onboarding_completed": True}

@router.post("/context/undo")
def undo_recent_context(req: ClearRequest):
    doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    previous = doc.get("previous_context")
    if not previous:
        return {"status": "nothing_to_undo", "current_context": doc.get("current_context")}
    current = doc.get("current_context", "")
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {
        "current_context": previous, "previous_context": current,
        "current_context_revision": int(doc.get("current_context_revision", 0)) + 1,
        "context_embedding": get_embedding(previous)}})
    return {"status": "success", "current_context": previous}

@router.get("/profile/memories")
def get_profile_memories(user_id: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {"profile_memory_preview": 1, "profile_memory_summary": 1}) or {}
    return {"memories": doc.get("profile_memory_preview", []), "summary": doc.get("profile_memory_summary", "")}

@router.post("/profile/memories/action")
def profile_memory_action(req: ProfileMemoryActionRequest):
    from services.memory_service import apply_memory_action
    try:
        return apply_memory_action(req.user_id, req.key, req.action, req.value)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

@router.post("/reset_deep_profile")
def reset_deep_profile(req: ClearRequest):
    """重置深層價值觀分析，讓使用者可以重新分析"""
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$unset": {"deep_profile": "", "dp_interaction_count": ""}}
    )
    return {"status": "success"}
