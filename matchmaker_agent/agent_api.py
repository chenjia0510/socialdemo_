import json
import os
import re
import sys
import time
from neo4j import GraphDatabase
from pathlib import Path
from dotenv import load_dotenv

# 強制抓取 agent_api.py 所在目錄下的 .env 檔案
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from pydantic import BaseModel
from matchmaker import MatchmakerAgent

# 初始化 FastAPI 應用程式 (就是這行剛剛不見了！)
app = FastAPI()

# 初始化我們的媒婆大腦
agent = MatchmakerAgent()

# 定義接收的資料格式
class MatchRequest(BaseModel):
    target_user: dict
    candidates: list
    target_deep_profile: dict = {}

def get_user_graph_memory(user_id: str) -> str:
    """從 Neo4j 讀取使用者的偏好與地雷"""
    step_start = time.perf_counter()
    URI = os.getenv("NEO4J_URI")
    AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
    
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            print(f"✅ [Neo4j 讀取] 連線驗證成功 (user={user_id})")
            with driver.session(database=DATABASE) as session:
                query = """
                MATCH (u:User {id: $user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                WHERE coalesce(r.active, true) = true
                RETURN coalesce(r.type, CASE WHEN r.stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END) AS type,
                       t.name AS trait, coalesce(r.reason, '') AS reason
                """
                result = session.run(query, user_id=user_id)
                
                memory_lines = []
                for record in result:
                    # 例如：[DISLIKES_TRAIT] 遇到特質「高外向」的對象。原因：...
                    memory_lines.append(f"[{record['type']}] 遇到特質「{record['trait']}」的對象。原因：{record['reason']}")
                
                if memory_lines:
                    print(f"[TIMING][9001 /api/match] Neo4j user memory: {time.perf_counter() - step_start:.3f}s lines={len(memory_lines)}")
                    return "\n".join(memory_lines)
                else:
                    print(f"[TIMING][9001 /api/match] Neo4j user memory: {time.perf_counter() - step_start:.3f}s lines=0")
                    return "目前圖庫中尚無該使用者的偏好或地雷紀錄。"
    except Exception as e:
        print(f"[TIMING][9001 /api/match] Neo4j user memory failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Neo4j user memory failed: {e}")
        return "無法讀取過往記憶。"

def get_global_rules() -> str:
    """從 Neo4j 讀取 Top 3 高權重的全域配對法則"""
    step_start = time.perf_counter()
    URI = os.getenv("NEO4J_URI")
    AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
    DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
    
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            print("✅ [Neo4j 全域法則] 連線驗證成功")
            with driver.session(database=DATABASE) as session:
                query = """
                MATCH (a:Agent {name: "System"})-[r:LEARNED_RULE]->(rule:GlobalRule)
                RETURN rule.content AS content, rule.category AS category, r.weight AS weight
                ORDER BY r.weight DESC
                LIMIT 3
                """
                result = session.run(query)
                
                rules = []
                for record in result:
                    rules.append(f"- [{record['category']}] {record['content']} (信心度：{record['weight']})")
                
                if rules:
                    print(f"[TIMING][9001 /api/match] Neo4j global rules: {time.perf_counter() - step_start:.3f}s rules={len(rules)}")
                    return "\n".join(rules)
                else:
                    print(f"[TIMING][9001 /api/match] Neo4j global rules: {time.perf_counter() - step_start:.3f}s rules=0")
                    return ""
    except Exception as e:
        print(f"[TIMING][9001 /api/match] Neo4j global rules failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Neo4j global rules failed: {e}")
        return ""

@app.post("/api/match")
async def match_endpoint(req: MatchRequest):
    total_start = time.perf_counter()
    print(f"[TIMING][9001 /api/match] start target={req.target_user.get('user_id')} candidates={len(req.candidates)}")
    print("📥 收到 V1 系統傳來的配對請求！")
    print("🧠 媒婆正在閱讀卷宗與圖譜記憶、進行多維度思考中...")
    
    # 1. 喚醒圖譜記憶（個體地雷）
    step_start = time.perf_counter()
    graph_memory = get_user_graph_memory(req.target_user.get("user_id"))
    print(f"[TIMING][9001 /api/match] get_user_graph_memory wrapper: {time.perf_counter() - step_start:.3f}s")
    print(f"📂 提煉出的記憶：\n{graph_memory}")

    # 阿月要同時理解雙方，而不是只拿發起者的地雷去猜候選人。
    enriched_candidates = []
    for candidate in req.candidates:
        enriched = dict(candidate)
        enriched["graph_memory"] = get_user_graph_memory(candidate.get("user_id"))
        enriched_candidates.append(enriched)
    
    # 2. 喚醒全域經驗法則
    step_start = time.perf_counter()
    global_heuristics = get_global_rules()
    print(f"[TIMING][9001 /api/match] get_global_rules wrapper: {time.perf_counter() - step_start:.3f}s")
    if global_heuristics:
        print(f"🌐 全域法則：\n{global_heuristics}")
    
    # 3. 交給 Agent 決策（傳入 graph_memory + global_heuristics）
    step_start = time.perf_counter()
    raw_response = agent.match(
        req.target_user, enriched_candidates, graph_memory,
        global_heuristics, req.target_deep_profile
    )
    print(f"[TIMING][9001 /api/match] agent.match LLM wrapper: {time.perf_counter() - step_start:.3f}s raw_chars={len(raw_response) if raw_response else 0}")
    
    try:
        step_start = time.perf_counter()
        # 嘗試把媒婆講的話解析成標準字典 (Dict)
        clean_response = raw_response.strip("` \n")
        if clean_response.lower().startswith("json"):
            clean_response = clean_response[4:].strip()
            
        parsed_data = json.loads(clean_response)
        
        # 🥚 雙黃蛋：支援 matches 陣列格式
        if "matches" in parsed_data and isinstance(parsed_data["matches"], list):
            match_ids = [m.get("matched_user_id", "?") for m in parsed_data["matches"]]
            print(f"Agent matched ids: {match_ids}")
            print(f"[TIMING][9001 /api/match] parse/return matches: {time.perf_counter() - step_start:.3f}s")
            print(f"[TIMING][9001 /api/match] total: {time.perf_counter() - total_start:.3f}s")
            return parsed_data
        else:
            # 向下相容：如果 LLM 仍回傳舊格式，包裝成 matches 陣列
            print(f"⚠️ LLM 回傳舊格式，自動包裝為 matches 陣列")
            single_match = {
                "matched_user_id": parsed_data.get("matched_user_id", "未知"),
                "contrast_label": "候選人",
                "recommendation_reason": parsed_data.get("recommendation_reason", ""),
                "receiver_reason": parsed_data.get("receiver_reason", parsed_data.get("recommendation_reason", "")),
                "distinctive_tags": parsed_data.get("distinctive_tags", [])
            }
            print(f"[TIMING][9001 /api/match] parse/return single fallback: {time.perf_counter() - step_start:.3f}s")
            print(f"[TIMING][9001 /api/match] total: {time.perf_counter() - total_start:.3f}s")
            return {"matches": [single_match]}
        
    except json.JSONDecodeError:
        print("⚠️ 媒婆沒有照格式輸出 JSON，啟動防呆機制！")
        print(f"原始回覆: {raw_response}")
        
        # 防呆機制：只抓名單上的第一位候選人
        fallback_matches = []
        for i, c in enumerate(req.candidates[:1]):
            fallback_matches.append({
                "matched_user_id": c.get("user_id", f"未知_{i}"),
                "contrast_label": f"候選人 {chr(65+i)}",
                "recommendation_reason": raw_response,
                "receiver_reason": raw_response,
                "distinctive_tags": []
            })
        print(f"[TIMING][9001 /api/match] parse failed fallback total: {time.perf_counter() - total_start:.3f}s")
        return {"matches": fallback_matches}



# 在 agent_api.py 中新增這段

class FeedbackRequest(BaseModel):
    user_id: str
    target_id: str # noqa
    action: str # "accept" 或 "decline"
    target_traits: dict # 對方的性格
    explicit_reasons: list[str] = []  # 使用者明確勾選的婉拒特質

# 這個變數暫時用來模擬 Agent 的記憶庫 (實戰中會存在向量資料庫或寫回 MongoDB)
agent_memory_db = {} 

@app.post("/api/feedback")
async def receive_feedback(req: FeedbackRequest):
    print(f"📥 媒婆收到回報：{req.user_id} 對 {req.target_id} 選擇了 {req.action}")
    
    # 初始化這個人的記憶本
    if req.user_id not in agent_memory_db:
        agent_memory_db[req.user_id] = {"history": [], "agent_reflection": "目前無特殊偏好。"}
        
    # 紀錄這次的事件（含明確婉拒原因）
    agent_memory_db[req.user_id]["history"].append({
        "action": req.action,
        "target_traits": req.target_traits,
        "explicit_reasons": req.explicit_reasons
    })
    
    # 🧠 【Agentic 行為：觸發反思與知識圖譜寫入】
    # 🌟 已經改成 >= 1，每次回饋都會立刻畫圖！
    if len(agent_memory_db[req.user_id]["history"]) >= 1:
        print("🤔 媒婆正在翻閱歷史紀錄，進行深度反思...")
        print(f"📋 [Debug] user_id={req.user_id}, history_count={len(agent_memory_db[req.user_id]['history'])}, explicit_reasons={req.explicit_reasons}")
        
        # 1. 整理歷史紀錄（含明確婉拒原因）
        history_text = ""
        for item in agent_memory_db[req.user_id]["history"]:
            reasons_str = ""
            if item.get("explicit_reasons"):
                reasons_str = f" | 明確婉拒原因：{', '.join(item['explicit_reasons'])}"
            history_text += f"- 行動：{item['action']} | 對方性格：{item['target_traits']}{reasons_str}\n"
            
        # 提取最新的 explicit_reasons 供圖譜大腦精準使用
        latest_explicit_reasons = req.explicit_reasons if req.explicit_reasons else []
        print(f"📋 [Debug] latest_explicit_reasons 傳入 LLM: {latest_explicit_reasons}")
        
        try:
            # 2. 呼叫圖譜大腦（傳入 history_text 與 explicit_reasons）
            raw_reflection_json = agent.generate_graph_reflection(history_text, explicit_reasons=latest_explicit_reasons)
            print(f"🧠 大腦萃取出的原始 JSON:\n{raw_reflection_json}")
            
            # 清理 JSON 字串防呆
            clean_json_str = raw_reflection_json.strip("` \n")
            if clean_json_str.lower().startswith("json"):
                clean_json_str = clean_json_str[4:].strip()
                
            reflection_data = json.loads(clean_json_str)
            
            # 3. 🌟 連線到 Neo4j 畫出泡泡圖！
            URI = os.getenv("NEO4J_URI")
            USERNAME = os.getenv("NEO4J_USERNAME")
            PASSWORD = os.getenv("NEO4J_PASSWORD")
            DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
            
            # === Debug 探測器 ===
            print("\n🔍 [Debug] 正在檢查 Neo4j 連線金鑰...")
            print(f"   - URI 狀態: {URI}")
            print(f"   - USERNAME 狀態: {USERNAME}")
            print(f"   - DATABASE 狀態: {DATABASE}")
            if PASSWORD is None:
                print("   - ❌ 嚴重錯誤：PASSWORD 讀取不到 (值為 None)！")
            else:
                print(f"   - ✅ PASSWORD 讀取成功，字元長度: {len(PASSWORD)}")
            print("==================================\n")
            # ============================
            
            AUTH = (USERNAME, PASSWORD)
            
            with GraphDatabase.driver(URI, auth=AUTH) as driver:
                driver.verify_connectivity()
                print("✅ [Neo4j 寫入] 連線驗證成功！")
                with driver.session(database=DATABASE) as session:
                    relationships = reflection_data.get("relationships", [])
                    if not relationships:
                        print("⚠️ LLM 回傳的 relationships 為空，沒有地雷需要寫入")
                    for rel in relationships:
                        # === 驗證 LLM 輸出格式與 Cypher 參數對接 ===
                        required_keys = ["trait", "relation_type"]
                        missing_keys = [k for k in required_keys if k not in rel or not rel[k]]
                        if missing_keys:
                            print(f"❌ Neo4j 寫入失敗: LLM 輸出缺少必要欄位 {missing_keys}，原始資料: {rel}")
                            continue
                        
                        trait_value = rel["trait"]
                        rel_type_value = rel["relation_type"]
                        reason_value = rel.get("reason", "")
                        
                        # === Debug：印出即將寫入的參數 ===
                        print(f"   🔹 準備寫入: user_id={req.user_id}, trait={trait_value}, rel_type={rel_type_value}, reason={reason_value}")
                        
                        try:
                            cypher_query = """
                            MERGE (u:User {id: $user_id})
                            MERGE (t:Trait {name: $trait})
                            MERGE (u)-[r:HAS_PREFERENCE {type: $rel_type, reason: $reason}]->(t)
                            """
                            result = session.run(cypher_query, 
                                        user_id=req.user_id,
                                        trait=trait_value,
                                        rel_type=rel_type_value,
                                        reason=reason_value)
                            # 消費結果以確保寫入完成
                            result.consume()
                            print(f"✨ 成功在資料庫畫出泡泡：({req.user_id}) -[{rel_type_value}]-> ({trait_value})")
                        except Exception as neo4j_err:
                            print(f"❌ Neo4j 寫入失敗: {neo4j_err}")
                            print(f"   失敗的參數: user_id={req.user_id}, trait={trait_value}, rel_type={rel_type_value}")

            # 反思完就清空收件匣
            agent_memory_db[req.user_id]["history"] = [] 
            
        except json.JSONDecodeError as json_err:
            print(f"❌ LLM 回傳 JSON 解析失敗: {json_err}")
            print(f"   原始回覆: {raw_reflection_json}")
        except Exception as e:
            print(f"❌ Neo4j 寫入失敗: {e}")
            
    return {"status": "success", "message": "媒婆已將此事記在心上。"}

# === 全域反思端點：配對成功時觸發 ===

class GlobalReflectionRequest(BaseModel):
    from_big_five: dict
    from_context: str = ""
    to_big_five: dict
    to_context: str = ""

@app.post("/api/global_reflection")
async def global_reflection_endpoint(req: GlobalReflectionRequest):
    print("🌐 收到全域反思請求！正在從成功配對中歸納通用法則...")
    
    try:
        # 1. 呼叫 LLM 歸納抽象化法則
        raw_response = agent.generate_global_reflection(
            from_big_five=req.from_big_five,
            from_context=req.from_context,
            to_big_five=req.to_big_five,
            to_context=req.to_context
        )
        print(f"🧠 全域反思原始回覆:\n{raw_response}")
        
        # 2. 清理 JSON 字串防呆
        clean_response = raw_response.strip("` \n")
        if clean_response.lower().startswith("json"):
            clean_response = clean_response[4:].strip()
            
        reflection_data = json.loads(clean_response)
        abstract_rule = reflection_data.get("abstract_rule", "")
        category = reflection_data.get("category", "情境型")
        
        if not abstract_rule:
            print("⚠️ LLM 未回傳有效的抽象法則，跳過寫入")
            return {"status": "skipped", "message": "無法歸納出有效的配對法則"}
        
        print(f"✨ 歸納出法則：[{category}] {abstract_rule}")
        
        # 3. 寫入 Neo4j：MERGE GlobalRule + weight 疊加
        URI = os.getenv("NEO4J_URI")
        AUTH = (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD"))
        DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
        
        try:
            with GraphDatabase.driver(URI, auth=AUTH) as driver:
                driver.verify_connectivity()
                print("✅ [Neo4j 全域法則寫入] 連線驗證成功！")
                with driver.session(database=DATABASE) as session:
                    cypher_query = """
                    MERGE (a:Agent {name: "System"})
                    MERGE (rule:GlobalRule {content: $abstract_rule})
                    ON CREATE SET rule.category = $category
                    MERGE (a)-[r:LEARNED_RULE]->(rule)
                    ON CREATE SET r.weight = 1
                    ON MATCH SET r.weight = r.weight + 1
                    """
                    result = session.run(cypher_query,
                               abstract_rule=abstract_rule,
                               category=category)
                    result.consume()
                    print(f"✅ 全域法則已寫入/更新：[{category}] {abstract_rule}")
        except Exception as neo4j_err:
            print(f"❌ Neo4j 寫入失敗 (全域法則): {neo4j_err}")
            return {"status": "error", "message": f"Neo4j 寫入失敗: {neo4j_err}"}
        
        return {"status": "success", "abstract_rule": abstract_rule, "category": category}
        
    except json.JSONDecodeError as e:
        print(f"⚠️ 全域反思 JSON 解析失敗：{e}")
        print(f"原始回覆：{raw_response}")
        return {"status": "error", "message": "JSON 解析失敗"}
    except Exception as e:
        print(f"⚠️ 全域反思處理失敗：{e}")
        return {"status": "error", "message": str(e)}


# === Conversation-derived preference memory ===

class MemoryObserveRequest(BaseModel):
    user_id: str
    text: str
    surface: str = "global"
    match_id: str | None = None

class MemoryActionRequest(BaseModel):
    user_id: str
    key: str
    action: str
    value: str | None = None


def _neo4j_config():
    return (os.getenv("NEO4J_URI"), (os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")), os.getenv("NEO4J_DATABASE", "neo4j"))

@app.post("/api/clear_graph")
async def clear_graph_endpoint():
    print("🧹 收到清空 Neo4j Graph 請求！")
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            with driver.session(database=DATABASE) as session:
                session.run("MATCH (n) DETACH DELETE n").consume()
        print("✅ Neo4j Graph 已完全清空！")
        return {"status": "success", "message": "Neo4j Graph已完全清空"}
    except Exception as e:
        print(f"❌ 清空 Neo4j Graph 失敗: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/memory/observe")
async def observe_memory(req: MemoryObserveRequest):
    prompt = f"""
只從使用者本人的第一人稱句子抽取可長期使用的交友偏好。
忽略他人的偏好、轉述、玩笑、假設、一次性情緒與近期行程。
健康、宗教、政治、性、創傷、精確位置等敏感資訊一律不要抽取，除非句中明確說「請記住」。
只回傳 JSON：{{"memories":[{{"key":"英文 snake_case canonical key","label":"2-8字繁體中文標籤","stance":"like|dislike|require|avoid","category":"lifestyle|habit|personality|relationship|activity","confidence":0.0}}]}}
只有 confidence >= 0.85 才放進 memories；沒有就回空陣列。
使用者句子：{req.text}
"""
    try:
        response = agent.client.chat.completions.create(model=agent.model, messages=[
            {"role": "system", "content": "你是保守、精準的偏好抽取器，只輸出 JSON。"},
            {"role": "user", "content": prompt}], temperature=0.0)
        raw = response.choices[0].message.content.strip(chr(96) + " \n")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        items = json.loads(raw).get("memories", [])
    except Exception as exc:
        print(f"Memory extraction failed: {exc}")
        return {"memories": []}
    allowed_stances = {"like", "dislike", "require", "avoid"}
    clean, now = [], time.time()
    URI, AUTH, DATABASE = _neo4j_config()
    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            with driver.session(database=DATABASE) as session:
                for item in items[:3]:
                    key = str(item.get("key", "")).strip().lower().replace(" ", "_")
                    label = re.sub(
                        r"^(?:喜歡|不喜歡|避免|需要|偏好|討厭)\s*[：:、，,]?\s*",
                        "", str(item.get("label", "")).strip()
                    )[:40]
                    stance = item.get("stance")
                    confidence = float(item.get("confidence", 0))
                    if not key or not label or stance not in allowed_stances or confidence < 0.85:
                        continue
                    category = str(item.get("category", "lifestyle"))[:30]
                    session.run("""
                        MERGE (u:User {id: $user_id}) MERGE (t:Trait {key: $key})
                        ON CREATE SET t.name=$label, t.category=$category
                        ON MATCH SET t.name=$label, t.category=$category
                        MERGE (u)-[r:HAS_PREFERENCE]->(t)
                        ON CREATE SET r.first_seen_at=$now, r.evidence_count=0
                        SET r.stance=$stance,
                            r.type=CASE WHEN $stance IN ['dislike','avoid'] THEN 'DISLIKES_TRAIT' ELSE 'LIKES_TRAIT' END,
                            r.confidence=CASE WHEN coalesce(r.confidence,0)>$confidence THEN r.confidence ELSE $confidence END,
                            r.evidence_count=coalesce(r.evidence_count,0)+1,
                            r.last_seen_at=$now, r.active=true, r.source=$surface
                    """, user_id=req.user_id, key=key, label=label, category=category, stance=stance,
                         confidence=confidence, now=now, surface=req.surface).consume()
                    clean.append({"key":key,"label":label,"stance":stance,"category":category,"confidence":confidence,"last_seen_at":now})
        return {"memories": clean}
    except Exception as exc:
        print(f"Memory graph write failed: {exc}")
        return {"memories": []}

@app.get("/api/memory/{user_id}")
async def list_memories(user_id: str, limit: int = 12):
    URI, AUTH, DATABASE = _neo4j_config()
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session(database=DATABASE) as session:
            rows = session.run("""
                MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait)
                WHERE coalesce(r.active,true)=true
                RETURN coalesce(t.key,toLower(replace(t.name,' ','_'))) AS key, t.name AS label,
                       coalesce(r.stance,CASE WHEN r.type='DISLIKES_TRAIT' THEN 'dislike' ELSE 'like' END) AS stance,
                       t.category AS category, coalesce(r.confidence,0.7) AS confidence,
                       coalesce(r.last_seen_at,0) AS last_seen_at
                ORDER BY confidence DESC,last_seen_at DESC LIMIT $limit
            """, user_id=user_id, limit=max(1,min(limit,30)))
            return {"memories":[dict(row) for row in rows]}

@app.post("/api/memory/action")
async def memory_action(req: MemoryActionRequest):
    if req.action not in {"disable","restore","correct"}:
        return {"status":"error","message":"unsupported action"}
    URI, AUTH, DATABASE = _neo4j_config()
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session(database=DATABASE) as session:
            row = session.run("""
                MATCH (u:User {id:$user_id})-[r:HAS_PREFERENCE]->(t:Trait {key:$key})
                SET r.active=$active,r.last_seen_at=$now,
                    t.name=CASE WHEN $value IS NULL OR $value='' THEN t.name ELSE $value END
                RETURN t.key AS key,t.name AS label,r.stance AS stance,t.category AS category,
                       r.confidence AS confidence,r.last_seen_at AS last_seen_at
            """, user_id=req.user_id,key=req.key,active=req.action!='disable',now=time.time(),value=req.value).single()
            return {"status":"success","memory":dict(row) if row else None}


if __name__ == "__main__":
    import uvicorn
    # 讓媒婆住在 9001 港口
    uvicorn.run(app, host="127.0.0.1", port=9001)
