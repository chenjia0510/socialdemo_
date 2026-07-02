# 媒人阿月邏輯

> 對應程式碼：`social_demotest/routers/chat.py`（全域阿月）、`matchmaker_agent/matchmaker.py`（配對決策阿月）、`social_demotest/services/ai_service.py`（性格分析阿月）

---

## 1. 阿月的三個面向

系統中「阿月」其實是三個不同位置的 LLM 角色，共用同一個人設但職責不同：

| 面向 | 程式位置 | LLM 管線 | 職責 |
| :--- | :--- | :--- | :--- |
| **全域阿月** | `chat.py` direct_chat | Ollama Cloud | 在 ai_assistant 聊天室陪聊、分析近況、觸發配對、主動關心 |
| **配對阿月** | `matchmaker.py` | OpenAI 相容 client | 在 9001 Agent 做配對決策、產生推薦理由 |
| **性格阿月** | `ai_service.py` | Ollama Cloud | big_five / deep_profile 對話收斂、情境摘要、破冰訊息 |

---

## 2. 共用人設（MEDIATOR_PERSONA）

`chat.py:44`：

```
你叫「阿月」，是一位閱人無數、溫暖又有觀察力的媒人。
你不是客服，說話自然、有分寸，偶爾善意吐槽；不施壓、不替人補故事。
請一律使用自然繁體中文，預設只回一句，必要時最多兩句，不用標題或條列。
```

配對阿月（`matchmaker.py`）有更完整的人設，額外加入角色錨定、Graph 邊界、決策鐵則等（見 MATCH_LOGIC.md）。

---

## 3. 說話風格（mediator_style）

`chat.py:71`，依使用者設定的 `mediator_tone` 切換：

| tone | 風格 |
| :--- | :--- |
| `friend`（預設） | 像熟朋友，口語、直率，偶爾說「欸」或「我跟你說」 |
| `gentle` | 像溫柔姐姐，穩定細膩、讓人安心，不說教 |
| `enthusiastic` | 像熱心媒婆，活潑有戲但不浮誇，不替任何人施壓 |

由 `system.py` 的 `POST /api/settings/mediator` 設定。

---

## 4. 全域阿月（ai_assistant 聊天室）

### 4.1 日常陪聊（direct_chat，對 `ai_assistant`）

`chat.py:655 direct_chat`，當 `contact_id == "ai_assistant"` 時：

1. **存訊息** → `messages_coll`
2. **LLM 分析**（`chat.py:881` prompt）：
   - 載入 `MEDIATOR_PERSONA` + `mediator_style`
   - 帶入使用者 big_five、current_context、graph_memories、accepted 關係的 evidence
   - LLM 回傳 JSON：`reply`、`conversation_intent`、`context_signals`、`context_should_update`、`context_confidence`、`context_summary`、`relationship_claims`
3. **情境更新判斷**：
   - `context_should_update=true` + `intent=recent_context` + `confidence≥0.85` → 更新 current_context
   - 使用者說「幫我更新近況」→ 強制更新
   - 使用者說「別記這段」→ 不更新
4. **readiness 計算** → 若 ≥75 且條件滿足 → 觸發配對（見 MATCH_LOGIC.md）
5. **記憶觀察** → `observe_user_memory` 萃取偏好寫入 Neo4j
6. **主動配對意圖偵測**：`classify_proposal_intent` 偵測「有興趣/先不要」等回應，直接呼叫 accept/decline

### 4.2 conversation_intent 分類

LLM 將每句話分類：

| intent | 意義 |
| :--- | :--- |
| `recent_context` | 使用者在說近況 |
| `relationship_chat` | 在聊已配對的對象 |
| `profile_fact` | 提供個人特質 |
| `casual_chat` | 閒聊 |
| `command` | 指令 |

只有 `recent_context` 會觸發 readiness 計算與情境更新。

### 4.3 主動關心（proactive_check）

`chat.py:1824`，前端每 5s 輪詢 `GET /api/proactive_check`：

**優先序**：
1. **memory_notices**：記憶觀察完成的通知（「我記住了：X」）
2. **mediator_inbox 事件**：阿月事件 queue 中優先序最高的事件
3. **主動關心**：若以上都沒有，且 `proactive_frequency` 設為秒數，則：
   - 條件：`last_activity > handled_activity` + `not conversation_active` + `current_time - last_activity >= freq_seconds`
   - LLM 產生一句主動關心訊息（帶 big_five.summary + current_context）

### 4.4 事件 queue（mediator_inbox）

`mediator_event_service.py`，阿月要對使用者說的話不是即時推播，而是寫進 `profiles.mediator_inbox`：

**優先序**：

| event_type | priority |
| :--- | :--- |
| match_proposal / incoming_match_interest | 100 |
| match_connected | 90 |
| probe_result / gentle_closure / mutual_interest | 80 |
| date_coordination_result | 75 |
| date_coordination_request / cancelled | 70 |
| match_search_failed / empty | 60 |
| match_search_blocked | 50 |
| feedback_consent_request | 45 |
| feedback_request | 35 |
| probe_question | 30 |
| （其他） | 40 |

`claim_next_mediator_event` 用 event_id 樂觀鎖避免多視窗重複領取。前端輪詢領取後渲染。

---

## 5. 配對阿月（9001 Agent）

### 5.1 決策流程

`matchmaker.py:78 match()`：

1. 組 payload：`target_user` + `candidates`（每位含 `graph_memory`）+ `target_deep_profile`
2. system_prompt 填入：
   - `[GRAPH_MEMORY_PLACEHOLDER]`：發起者本人的地雷與偏好
   - `[GLOBAL_HEURISTICS_PLACEHOLDER]`：全域法則 top 3
   - `[DEEP_PROFILE_PLACEHOLDER]`：發起者深層價值觀
3. LLM 回傳純 JSON `matches[]`（含 matched_user_id、recommendation_reason、receiver_reason、distinctive_tags、score_breakdown、top_reasons）

### 5.2 角色錨定（system_prompt 重點）

- 你本人就是阿月，對發起者用「你」、候選人用「他/她」
- 絕對不要用「嘿阿月」稱呼對方（因為你自己就是阿月）
- recommendation_reason：由阿月對發起者說
- receiver_reason：由阿月對接收者說

### 5.3 Graph 雙向檢查

- `[GRAPH_MEMORY_PLACEHOLDER]` = 發起者本人的地雷
- 每位 candidate dict 自帶 `graph_memory` = 候選人本人的地雷
- prompt 明確要求雙向比對、不可搞錯邊
- DISLIKES_TRAIT 命中對方特質 → 絕對不推薦

### 5.4 資料歸屬提醒

prompt 明確標示：
- `current_context` / `context_signals`：屬於該 user 本人
- `big_five` / `big_five.summary`：屬於該 user 本人
- `deep_profile`：屬於該 user 本人
- `graph_memory`：屬於該 user 本人
- 寫理由時不可張冠李戴

### 5.5 防呆機制

`agent_api.py:171`，LLM 回傳非 JSON 時：
- 抓名單第一位候選人
- 用 raw_response 當 reason
- 仍包裝成 `matches[]` 格式回傳

---

## 6. 性格阿月（ai_service.py）

### 6.1 Big Five 收斂（`analyze_big_five`）

`ai_service.py:52`：

- 5 輪以內的情境對話，每輪微調 O/C/E/A/N 與 summary
- 第 5 輪強制 `is_complete=true` 做總結
- 每次只丟一個情境題，不提供選項
- 第一輪以 `initial_interest` 切入

### 6.2 深層價值觀（`analyze_deep_profile`）

`ai_service.py:206`：

- 在 Big Five 完成後進行
- 探索：核心價值觀、人生目標、關係需求、壓力應對、理想未來
- 5 輪強制完成
- 第一輪從近期情境/興趣切入，不直接問「最重要的事」

### 6.3 情境摘要（`summarize_context`）

`ai_service.py:164`：

- 將使用者訊息與先前情境融合
- 產生 10-15 字摘要
- 注意時態（已發生 vs 想去）
- 寫入 `profiles.current_context`

### 6.4 破冰訊息（`generate_peer_first_message`）

`ai_service.py:149`：

- 配對成功時以「發起者性格」對接收者發第一句
- 讀 big_five + reason + 對方 current_context
- 存入 messages_col

---

## 7. 阿月的記憶學習

### 7.1 觀察（observe_user_memory）

`memory_service.py:108`：

- 使用者聊天中的第一人稱偏好
- LLM 萃取 confidence≥0.85 的特質
- 寫入 Neo4j `HAS_PREFERENCE`
- 更新 `profile_memory_preview` 與 `memory_notices`
- 推播「我記住了：X」通知

### 7.2 個體反思（婉拒時）

`agent_api.py:202 /api/feedback`：

- 使用者婉拒配對時觸發
- LLM 從回饋萃取 DISLIKES_TRAIT
- 寫入 Neo4j，下次配對避開

### 7.3 全域反思（配對成功時）

`agent_api.py:326 /api/global_reflection`：

- 歸納抽象化通用法則
- 寫入 `GlobalRule`，weight 疊加
- 下次配對 LLM 參考 top 3

---

## 8. 阿月與使用者的接觸點

| 介面 | 阿月在做什麼 |
| :--- | :--- |
| **ai_assistant 聊天室**（全域） | 陪聊、分析近況、觸發配對、主動關心、配對提案卡、收邀請卡 |
| **私訊阿月**（關係內） | 回答關於配對對象的問題、探口風、約會協調、回饋收集（見 PRIVATE_MEDIATOR_LOGIC.md） |
| **配對決策**（9001 內部） | 從 5 個候選人選 1 位、產生雙視角理由、雙向地雷檢查 |
| **性格對話**（onboarding） | Big Five / deep_profile 收斂 |
| **記憶觀察**（背景） | 從聊天萃取偏好寫 Graph |
| **反思**（背景） | 婉拒時萃取地雷、成功時歸納法則 |