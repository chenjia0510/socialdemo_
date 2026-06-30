import os
import json
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

class MatchmakerAgent:
    def __init__(self):
        # 初始化 LLM 客戶端
        self.client = OpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL")
        )
        self.model = os.getenv("LLM_MODEL_ID")
        
        # 【SOUL：阿月的人設與靈魂】
        self.system_prompt = """你叫「阿月」，是一位在台灣校園打滾多年、閱人無數的超強 AI 媒人。
        你不是客氣疏離的客服。你的口吻像熟悉雙方很久的朋友：溫暖、觀察細膩、熱心直率，
        偶爾可以善意吐槽，但絕不刻薄，也絕不向任何人施壓。
        推薦理由要像真人媒人主動牽線，指出兩人近期生活中具體、細微、可以立刻開聊的連結。
        例如：「我今天看到這個人，立刻就想到你！你們最近都在找老咖啡廳，而且他剛去過你清單上的那間。」
        如果資料不足，不可捏造店名、旅行或經歷；改用現有資料中最具體的共同點。

        你的任務是閱讀一位「目標用戶」以及「候選人名單」的狀態與特徵，選出「唯一一位」最值得此刻牽線的候選人。
        必須以近期情境的具體共通點為主，再檢查性格與價值觀是否適合；不要一次提供選擇題。

        【⚠️ 絕對遵守的輸出格式】
        請務必只輸出純 JSON 格式，不要包含任何 Markdown 標記（如 ```json）或其他多餘文字。
        必須包含以下結構：
        {
            "matches": [
                {
                    "matched_user_id": "唯一候選人的 user_id",
                    "contrast_label": "用 4-6 個字描述這位候選人的性格風格（如：外向活潑型）",
                    "recommendation_reason": "一段約 50 字的推薦理由，以『你』稱呼發起者、以『他/她』稱呼候選人，溫暖、幽默且一針見血",
                    "receiver_reason": "一段約 50 字的理由，但視角反轉：以『你』稱呼候選人（接收者），說明『有人因為你的什麼特質加上他現在想做什麼，所以想認識你』，語氣溫暖且真誠",
                    "distinctive_tags": ["請輸出 5-6 個短句；必須綜合候選人的 current_context、initial_interest、big_five.summary 與 deep_profile（價值觀、關係需求、壓力因應、未來想像），萃取最特殊、強烈、具主導性、或可能成為他人拒絕理由的具體特徵/狀態。範例：面對衝突時直接表達情緒、實力至上主義、掌控欲較強、近期去非洲看秀體驗不佳。格式限制：陣列裡只能放精煉短句，絕對不要加『鮮明特質：』『近期情境：』『興趣：』『價值觀：』等任何前綴分類詞。"]
                }
            ]
        }

        【🎯 人物標籤 (contrast_label) 的要求】
        - 用 4~6 個字寫出候選人最鮮明、友善的人物風格
        - 範例：「熱血行動派」、「溫柔傾聽型」、「沉穩可靠型」

        【🧠 極重要：圖譜大腦過往記憶 (地雷與偏好)】
        以下是該使用者過去拒絕或接受對象時，大腦萃取出的核心知識：
        [GRAPH_MEMORY_PLACEHOLDER]

        【🧠 全域經驗法則 (Global Heuristics)】
        以下是系統從過往所有成功配對中歸納出的通用法則，請在推薦時優先參考這些經驗：
        [GLOBAL_HEURISTICS_PLACEHOLDER]

        【🎯 深層價值觀分析 (Deep Profile)】
        如果目標用戶有 deep_profile 資料，這是比 Big Five 更深層的價值觀洞察，包含人生哲學、依附傾向、決策風格等。請將此作為配對判斷的「核心依據」，權重高於 Big Five 分數：
        [DEEP_PROFILE_PLACEHOLDER]

        【⚠️ 決策鐵則】
        如果過往記憶中包含 [DISLIKES_TRAIT]，請「絕對避免」選擇具有該特質的候選人，即使他們的相似度分數很高！這是最高扣分項目！
        請在「推薦理由 (recommendation_reason)」中，主動提及你是因為參考了過往記憶（例如：「記得你之前說過不喜歡太外向的，所以我幫你挑了...」），讓使用者感受到你有在學習！"""

    def match(self, target_user, candidates, graph_memory="", global_heuristics="", target_deep_profile=None):
        total_start = time.perf_counter()
        # 【卷宗打包：將資料整理成 Agent 看得懂的格式】
        payload = {
            "target_user": target_user,
            "candidates": candidates,
            "graph_memory": graph_memory
        }
        
        # 如果有 deep_profile 資料，加入 payload
        if target_deep_profile:
            payload["target_deep_profile"] = target_deep_profile
            # 也把每位 candidate 的 deep_profile 加入（如果有的話）
            for c in candidates:
                if c.get("deep_profile"):
                    # 確保 candidates 在 payload 中也帶有 deep_profile
                    pass  # candidates 已經包含 deep_profile 欄位
        
        # 安全地替換記憶，避免 JSON 大括號衝突
        memory_text = graph_memory if graph_memory else "目前圖庫中尚無該使用者的偏好或地雷紀錄。"
        system_content = self.system_prompt.replace("[GRAPH_MEMORY_PLACEHOLDER]", memory_text)
        
        # 替換全域經驗法則
        heuristics_text = global_heuristics if global_heuristics else "目前尚無累積的通用法則。"
        system_content = system_content.replace("[GLOBAL_HEURISTICS_PLACEHOLDER]", heuristics_text)
        
        # 替換深層價值觀分析
        if target_deep_profile:
            deep_profile_text = json.dumps(target_deep_profile, ensure_ascii=False, indent=2)
        else:
            deep_profile_text = "目前尚無深層價值觀分析資料，請以 Big Five 和情境為主要判斷依據。"
        system_content = system_content.replace("[DEEP_PROFILE_PLACEHOLDER]", deep_profile_text)
        
        print("🧠 媒婆正在閱讀卷宗與圖譜記憶、進行多維度思考中...")
        try:
            payload_text = json.dumps(payload, ensure_ascii=False)
            print(
                "[TIMING][MatchmakerAgent.match] before LLM "
                f"system_chars={len(system_content)} payload_chars={len(payload_text)} "
                f"candidates={len(candidates)}"
            )
            step_start = time.perf_counter()
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": payload_text}
                ],
                temperature=0.7 # 稍微給一點創意空間
            )
            content = response.choices[0].message.content
            print(
                "[TIMING][MatchmakerAgent.match] LLM call: "
                f"{time.perf_counter() - step_start:.3f}s output_chars={len(content) if content else 0}"
            )
            print(f"[TIMING][MatchmakerAgent.match] total: {time.perf_counter() - total_start:.3f}s")
            return content
        except Exception as e:
            print(f"[TIMING][MatchmakerAgent.match] failed after {time.perf_counter() - total_start:.3f}s")
            return f"❌ 媒婆罷工了，錯誤訊息：{e}"

    def generate_graph_reflection(self, history_text, explicit_reasons=None):
        print(f"🧠 [Debug] generate_graph_reflection called with explicit_reasons={explicit_reasons}")
        # 如果使用者有明確勾選婉拒原因，加入高權重提示
        explicit_section = ""
        if explicit_reasons:
            reasons_bullets = "\n".join(f"  - {r}" for r in explicit_reasons)
            explicit_section = f"""
        【🔴 使用者明確勾選的婉拒原因（最高優先級！）】
        使用者從候選人的「動態標籤」中，親自勾選了以下項目作為婉拒理由。
        這些標籤通常已經是純短句，例如「面對衝突時直接表達情緒」「實力至上主義」「掌控欲較強」。
        請將它們精煉為簡短的特質名詞；如果遇到舊資料仍帶分類前綴，先移除前綴，只保留核心特質。
        請「務必」將這些項目轉為 DISLIKES_TRAIT 關係，這是比任何推測都更可靠的偏好訊號：
{reasons_bullets}
        """

        reflection_prompt = f"""
        你是一位專業的心理分析師與知識圖譜工程師。
        請閱讀這位使用者的「最近配對回饋紀錄」。
        你的任務是從中萃取出使用者對特定性格特質的「地雷 (DISLIKES_TRAIT)」。
        
        ⚠️ 重要：只輸出 DISLIKES_TRAIT（使用者不喜歡的特質），不要輸出 LIKES_TRAIT。
        我們只記錄地雷，不記錄偏好。偏好會由全域反思系統另外處理。
        
        ⚠️ 特別注意：傳入的婉拒理由包含候選人的「專屬動態標籤」。
        新版標籤應是沒有分類前綴的純短句；請直接萃取核心特質。若遇到舊資料仍帶分類前綴，請先移除前綴，只保留核心特質。
        
        【最近的配對回饋】
        {history_text}
        {explicit_section}
        【⚠️ 絕對遵守的輸出格式】
        請務必只輸出純 JSON 格式，不要包含任何 Markdown 標記（如 ```json）或其他多餘文字。
        如果沒有明確的地雷，可以回傳空的陣列 []。
        請根據以下結構輸出：
        {{
            "relationships": [
                {{
                    "user_id": "發起動作的使用者 ID",
                    "relation_type": "DISLIKES_TRAIT",
                    "trait": "特質名稱（請精煉為2-4字簡短名詞，例如：冒險精神、愛打籃球、高外向、穩重）",
                    "reason": "為什麼討厭的具體原因摘要"
                }}
            ]
        }}
        """
        
        try:
            print("🧠 大腦正在將流水帳轉換為知識圖譜結構...")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一個精準的 JSON 知識抽取引擎。"},
                    {"role": "user", "content": reflection_prompt}
                ],
                temperature=0.1
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"{{\"error\": \"反思失敗：{e}\"}}"

    def generate_global_reflection(self, from_big_five, from_context, to_big_five, to_context):
        """從一次成功的配對中，歸納出抽象化的全域配對法則"""
        global_prompt = f"""
        你是一位資深的心理學家與配對策略師。
        剛剛有一對配對成功了（雙方都接受了彼此），以下是兩人的性格與情境資料：

        【發起者 (A)】
        性格 Big Five：{json.dumps(from_big_five, ensure_ascii=False) if isinstance(from_big_five, dict) else from_big_five}
        近期情境：{from_context}

        【接受者 (B)】
        性格 Big Five：{json.dumps(to_big_five, ensure_ascii=False) if isinstance(to_big_five, dict) else to_big_five}
        近期情境：{to_context}

        請從這次成功的配對中，歸納出一條「抽象化」的配對法則。
        這條法則應是通用的，不要包含特定人的名字或 ID，而是提煉出「什麼性格特質的人，在什麼情境下，適合跟什麼特質的人配對」的規律。

        ⚠️ 絕對遵守的輸出格式：
        請務必只輸出純 JSON 格式，不要包含任何 Markdown 標記（如 ```json）或其他多餘文字。
        請根據以下結構輸出：
        {{
            "abstract_rule": "一條通用的配對法則（例如：情緒低落的人適合與高親和性、低神經質的人配對）",
            "category": "互補型 / 相似型 / 情境型（擇一）"
        }}
        """
        
        try:
            print("🧠 媒婆正在從成功配對中歸納全域法則...")
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一個精準的 JSON 配對法則萃取引擎，專門從成功配對案例中歸納抽象化的通用法則。"},
                    {"role": "user", "content": global_prompt}
                ],
                temperature=0.3
            )
            return response.choices[0].message.content
        except Exception as e:
            return f'{{"error": "全域反思失敗：{e}"}}'

# === 獨立測試區塊 ===
if __name__ == "__main__":
    agent = MatchmakerAgent()
    
    # 模擬 FastAPI 傳來的：使用者的近期動態
    test_user = {
        "current_context": "剛被老闆念心情極度低落，想找人吃消夜抱怨一下",
        "interests": ["打電動", "美食"]
    }
    
    # 模擬 ChromaDB 篩選出來的：Top 3 候選人
    test_candidates = [
        {"candidate_id": "1號", "traits": "外向暖男，擅長傾聽，目前肚子很餓想吃東西", "interests": ["打電動", "籃球"]},
        {"candidate_id": "2號", "traits": "內向，喜歡看書，目前準備睡覺", "interests": ["閱讀", "咖啡"]},
        {"candidate_id": "3號", "traits": "今天也被老闆罵，心情極差，不想講話", "interests": ["美食", "看劇"]}
    ]
    
    # 啟動配對
    result = agent.match(test_user, test_candidates)
    
    print("\n💌 媒婆的最終推薦信：\n")
    print("=" * 40)
    print(result)
    print("=" * 40)
