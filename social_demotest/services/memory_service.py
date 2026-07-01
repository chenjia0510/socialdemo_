import json
import re
import time
import requests
from pathlib import Path
from database import profiles_coll

AGENT_URL = "http://127.0.0.1:9001"

MEMORY_PREFIX_RE = re.compile(r"^(?:喜歡|不喜歡|避免|需要|偏好|討厭)\s*[：:、，,]?\s*")


def normalize_memory_item(item: dict) -> dict:
    clean = dict(item or {})
    label = str(clean.get("label", "")).strip()
    while label and MEMORY_PREFIX_RE.match(label):
        label = MEMORY_PREFIX_RE.sub("", label, count=1).strip()
    clean["label"] = label
    return clean


def memory_summary(items: list[dict]) -> str:
    labels = {"dislike": "不喜歡", "avoid": "避免", "require": "需要", "like": "喜歡"}
    return "、".join(
        labels.get(item.get("stance"), "喜歡") + normalize_memory_item(item).get("label", "")
        for item in items[:8] if normalize_memory_item(item).get("label")
    )[:300]


def get_user_graph_memories(user_id: str, limit: int = 20) -> list[dict]:
    """Return active Graph preferences as owner-scoped structured facts."""
    try:
        response = requests.get(f"{AGENT_URL}/api/memory/{user_id}", params={"limit": limit}, timeout=12)
        response.raise_for_status()
        items = response.json().get("memories", [])
    except Exception:
        try:
            from neo4j import GraphDatabase
            uri, auth, database = _agent_graph_config()
            with GraphDatabase.driver(uri, auth=auth) as driver:
                with driver.session(database=database) as session:
                    rows = session.run("""
                        MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                        WHERE coalesce(r.active,true)=true
                        RETURN coalesce(t.key,toLower(replace(t.name,' ','_'))) AS key,
                               t.name AS label, coalesce(r.stance,'like') AS stance,
                               t.category AS category, coalesce(r.confidence,0.7) AS confidence,
                               coalesce(r.last_seen_at,0) AS last_seen_at
                        ORDER BY confidence DESC,last_seen_at DESC LIMIT $limit
                    """, user_id=user_id, limit=max(1, min(limit, 30)))
                    items = [dict(row) for row in rows]
        except Exception:
            items = []
    return [
        {**normalize_memory_item(item), "owner_user_id": user_id}
        for item in items if normalize_memory_item(item).get("label")
    ]

def _agent_graph_config():
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).resolve().parents[2] / "matchmaker_agent" / ".env")
    return env.get("NEO4J_URI"), (env.get("NEO4J_USERNAME"), env.get("NEO4J_PASSWORD")), env.get("NEO4J_DATABASE", "neo4j")


def _observe_direct(user_id: str, text: str, surface: str):
    from neo4j import GraphDatabase
    from services.ai_service import generate_chat_completion
    prompt = f"""只從使用者本人的第一人稱句子抽取穩定交友偏好。忽略轉述、玩笑、假設、近期行程和敏感資訊。
只回 JSON：{{"memories":[{{"key":"英文snake_case","label":"繁中短標籤","stance":"like|dislike|require|avoid","category":"habit|lifestyle|personality|relationship|activity","confidence":0.0}}]}}。
只有信心 >=0.85 才輸出。句子：{text}"""
    data = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
    items, clean, now = data.get("memories", [])[:3], [], time.time()
    uri, auth, database = _agent_graph_config()
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            for item in items:
                key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                label = normalize_memory_item(item).get("label", "")[:40]
                stance = item.get("stance")
                confidence = float(item.get("confidence", 0))
                if not key or not label or stance not in {"like", "dislike", "require", "avoid"} or confidence < 0.85:
                    continue
                category = str(item.get("category", "lifestyle"))[:30]
                session.run("""MERGE (u:User {id:$user_id}) MERGE (t:Trait {key:$key})
                    ON CREATE SET t.name=$label,t.category=$category ON MATCH SET t.name=$label,t.category=$category
                    MERGE (u)-[r:HAS_PREFERENCE]->(t) ON CREATE SET r.first_seen_at=$now,r.evidence_count=0
                    SET r.stance=$stance,r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                    r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                    r.evidence_count=coalesce(r.evidence_count,0)+1,r.last_seen_at=$now,r.active=true,r.source=$surface""",
                    user_id=user_id,key=key,label=label,category=category,stance=stance,confidence=confidence,now=now,surface=surface).consume()
                clean.append({"key":key,"label":label,"stance":stance,"category":category,"confidence":confidence,"last_seen_at":now})
    return clean


def _action_direct(user_id: str, key: str, action: str, value: str | None):
    from neo4j import GraphDatabase
    uri, auth, database = _agent_graph_config()
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            row = session.run("""MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait {key:$key})
                SET r.active=$active,r.last_seen_at=$now,t.name=CASE WHEN $value IS NULL OR $value='' THEN t.name ELSE $value END
                RETURN t.key AS key,t.name AS label,r.stance AS stance,t.category AS category,r.confidence AS confidence,r.last_seen_at AS last_seen_at""",
                user_id=user_id,key=key,active=action!='disable',now=time.time(),value=value).single()
            return {"status":"success","memory":dict(row) if row else None}



def observe_user_memory(user_id: str, text: str, surface: str, match_id: str | None = None):
    """Extract only first-person durable preferences; raw text is never persisted."""
    try:
        response = requests.post(f"{AGENT_URL}/api/memory/observe",
            json={"user_id": user_id, "text": text, "surface": surface, "match_id": match_id}, timeout=45)
        if response.status_code == 404:
            learned = _observe_direct(user_id, text, surface)
        else:
            response.raise_for_status()
            learned = response.json().get("memories", [])
        if not learned:
            return []
        doc = profiles_coll.find_one({"user_id": user_id}, {"profile_memory_preview": 1}) or {}
        preview = {
            item.get("key"): normalize_memory_item(item)
            for item in doc.get("profile_memory_preview", []) if item.get("key")
        }
        for item in learned:
            preview[item["key"]] = normalize_memory_item(item)
        compact = sorted(preview.values(), key=lambda x: x.get("last_seen_at", 0), reverse=True)[:12]
        summary = memory_summary(compact)
        events = [{
            "type": "memory_learned",
            "message": f"我記住了：{item.get('label')}。記錯的話可以在設定裡撤銷。",
            "memory": normalize_memory_item(item),
            "created_at": time.time()
        } for item in learned]
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$set": {"profile_memory_preview": compact, "profile_memory_summary": summary},
             "$push": {"memory_notices": {"$each": events}}},
            upsert=True
        )
        return learned
    except Exception as exc:
        print(f"Memory observation skipped: {exc}")
        return []


def apply_memory_action(user_id: str, key: str, action: str, value: str | None = None):
    response = requests.post(f"{AGENT_URL}/api/memory/action",
        json={"user_id": user_id, "key": key, "action": action, "value": value}, timeout=30)
    result = _action_direct(user_id, key, action, value) if response.status_code == 404 else response.json()
    if response.status_code != 404:
        response.raise_for_status()
    doc = profiles_coll.find_one({"user_id": user_id}, {"profile_memory_preview": 1}) or {}
    preview = [normalize_memory_item(item) for item in doc.get("profile_memory_preview", [])]
    if action == "disable":
        preview = [item for item in preview if item.get("key") != key]
    elif action in {"restore", "correct"} and result.get("memory"):
        preview = [item for item in preview if item.get("key") != key] + [
            normalize_memory_item(result["memory"])
        ]
    preview = preview[:12]
    profiles_coll.update_one({"user_id": user_id}, {"$set": {
        "profile_memory_preview": preview,
        "profile_memory_summary": memory_summary(preview),
    }})
    return result
