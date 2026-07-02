# 阿月 AI 媒人系統 — 架構分析

> 本文件由程式碼靜態分析產生，僅供參考，未修改任何程式碼。

---

## 1. 系統總覽

系統由兩個獨立服務組成，透過 HTTP 互相溝通：

```
┌──────────────────────────────────────────────────────────────┐
│  使用者瀏覽器                                                 │
│  (frontend.html, 單一 HTML 檔，Tailwind + 原生 JS)            │
└──────────────────────────────────────────────────────────────┘
        │  HTTP (port 8000)
        ▼
┌──────────────────────────────────────────────────────────────┐
│  V1 Backend  (social_demotest/)   port 8000                  │
│  FastAPI + MongoDB Atlas + Google Embedding + Ollama LLM     │
│                                                              │
│  職責：使用者畫像、配對管線、聊天、媒人事件、輪詢通知        │
└──────────────────────────────────────────────────────────────┘
        │  HTTP (port 9001)
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Matchmaker Agent  (matchmaker_agent/)   port 9001           │
│  FastAPI + Neo4j + OpenAI 相容 LLM                            │
│                                                              │
│  職責：LLM 配對決策、Graph 記憶讀寫、Graph 反思、全域法則    │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│  外部資料層                                                   │
│  ┌─────────────┐   ┌───────────┐   ┌──────────────────────┐ │
│  │ MongoDB Atlas│   │ Neo4j     │   │ LLM (Ollama / Google)│ │
│  │ (profiles,   │   │ (User-    │   │  - chat / embedding  │ │
│  │  matches,    │   │  Trait    │   │  - matchmaker prompt │ │
│  │  messages)  │   │  graph)   │   │                      │ │
│  └─────────────┘   └───────────┘   └──────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

**啟動順序**：先啟動 9001 Agent，再啟動 8000 Backend（由 `run_demo.bat` 自動處理）。
8000 Backend 在配對時會打 `http://127.0.0.1:9001/api/match` 把候選人清單交給 Agent 決策。

---

## 2. 資料夾結構

```
專案2/
├── run_demo.bat                  # 同時啟動兩個服務的批次檔
├── social_demotest/              # ===== V1 Backend (port 8000) =====
│   ├── main.py                   # FastAPI app 入口，掛載 4 個 router
│   ├── config.py                 # 由 .env 讀取 Mongo/Google/Ollama 設定
│   ├── database.py               # MongoDB 連線與三個 collection
│   ├── models.py                 # Pydantic 請求模型
│   ├── frontend.html             # 前端單頁（同時是 API server 吐出的 /）
│   ├── matchmaking_logic.md      # 配對邏輯舊版文件
│   ├── tags.json                 # 大五人格 tag 設定
│   ├── routers/
│   │   ├── frontend.py           # GET /  回傳 frontend.html
│   │   ├── chat.py               # 聊天、聯絡人、媒人私訊、關係測驗 (32 routes)
│   │   ├── match.py              # 配對管線、accept/decline、狀態查詢
│   │   └── system.py             # 初始化、seed、通知、設定、記憶管理
│   └── services/
│       ├── ai_service.py         # Google embedding + Ollama chat、Big Five/深層分析、配對 fallback、破冰訊息、情境摘要
│       ├── chat_service.py       # room_id 產生、訊息存 MongoDB
│       ├── mediator_event_service.py  # 阿月事件 queue（優先序排序）
│       └── memory_service.py     # Graph 記憶觀察/動作（委派 9001，含 fallback 直連 Neo4j）
│
└── matchmaker_agent/            # ===== Matchmaker Agent (port 9001) =====
    ├── agent_api.py              # FastAPI app；/api/match、/api/feedback、Graph 端點
    ├── matchmaker.py             # MatchmakerAgent 類別：system_prompt + LLM 呼叫、反思
    ├── test.py                   # 獨立測試
    └── .env                      # Neo4j + LLM 金鑰
```

---

## 3. 資料層

### 3.1 MongoDB（由 8000 直接操作）

`database.py` 定義三個 collection：

| Collection | 內容 | 主要寫入者 |
| :--- | :--- | :--- |
| `profiles` | 使用者畫像：`user_id`、`big_five`、`deep_profile`、`current_context`、`context_embedding`、`context_signals`、`distinctive_tags`、`match_search`(狀態機)、`active_match_proposal_id`、`mediator_inbox`(事件 queue)、`profile_memory_preview`、`proactive_frequency` 等 | chat.py、system.py、match.py |
| `matches` | 配對紀錄：`from_user`、`to_user`、`status`、`reason`、`receiver_reason`、`score_breakdown`、`reason_items`、`match_context_snapshot`、`created_at` 等 | match.py |
| `messages` | 聊天訊息：`room_id`、`sender_id`、`content`、`message_type`、`metadata` | chat_service.py |

`profiles` 同時肩負「使用者畫像」與「配對狀態機」兩個職責，欄位相當多。

### 3.2 Neo4j（由 9001 直接操作，8000 透過 9001 間接）

知識圖譜結構：

```
(User {id}) -[HAS_PREFERENCE {stance,type,reason,confidence,active,source}]-> (Trait {key,name,category})
(Agent {name:"System"}) -[LEARNED_RULE {weight}]-> (GlobalRule {content,category})
```

- `HAS_PREFERENCE`：使用者本人的偏好/地雷，`stance` ∈ {like, dislike, require, avoid}，`type` 衍生為 `LIKES_TRAIT` / `DISLIKES_TRAIT`。
- `LEARNED_RULE`：系統從成功配對歸納出的全域法則，weight 疊加。

8000 Backend 透過 `memory_service.py` 呼叫 9001 的 REST 端點操作 Graph；當 9001 不可用時有 fallback 直接連 Neo4j（`_observe_direct` / `_action_direct`）。

### 3.3 LLM 服務

8000 Backend（`ai_service.py`）：
- **Embedding**：Google AI Studio `gemini-embedding-2`（`get_embedding`），用於 `context_embedding` 與 MongoDB Atlas Vector Search。
- **Chat**：Ollama Cloud（`OLLAMA_CHAT_MODEL`，預設 `gemini-3-flash-preview:cloud`）—用於 Big Five/深層分析、情境摘要、配對 fallback、破冰訊息。

9001 Agent（`matchmaker.py`）：
- 獨立的 `OpenAI` client（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID`），與 8000 用的 Ollama 不同管線，主要做配對決策與反思。

---

## 4. Backend (8000) 詳細

### 4.1 Routers

| Router | Prefix | 主要職責 |
| :--- | :--- | :--- |
| `frontend.py` | (root) | `GET /` 回傳 `frontend.html` |
| `chat.py` | `/api` | 聊天、聯絡人、媒人私訊、關係測驗、主動配對檢查（14 routes） |
| `match.py` | `/api/match` | 配對管線、accept/decline、狀態查詢、取消（6 routes） |
| `system.py` | `/api` | 初始化、seed、清除、通知、設定、記憶管理（11 routes） |

### 4.2 配對管線（`match.py` 核心流程）

`generate_matches_for_user` 是主流程：

```
1. 載入 user profile + 確保 context_embedding
2. 載入既有 matches，組 excluded_users（accepted/draft/pending/近期 declined）
3. MongoDB Atlas $vectorSearch（top 5 候選人，近況向量相似）
4. 補上每位候選人的 deep_profile
5. POST http://127.0.0.1:9001/api/match
   ├─ target_user + candidates + target_deep_profile
   └─ Agent 回傳 matches[]（含 matched_user_id, recommendation_reason, receiver_reason, distinctive_tags, score_breakdown）
6. build_validated_match_explanation（target→candidate 與 candidate→target 雙向）
   ├─ 查雙方 Graph 記憶（get_user_graph_memories）
   ├─ 算 shared_traits / conflicts / shared_values / shared_context / Big Five 距離
   └─ 產 reason_items（owner-bound evidence）+ score_breakdown
7. 組合 ai_recommendation_reason / ai_receiver_reason
8. 寫入 matches_coll（status=draft, delivery_channel=mediator_chat）
9. 回傳 result_matches + debug_info
```

### 4.3 配對狀態機（`matches.status`）

```
draft  ──(發起者 accept)──▶ pending  ──(接收者 accept)──▶ accepted
  │                          │
  └──(發起者 decline)──▶ declined   └──(接收者 decline)──▶ declined

draft / pending 超過 TTL → expired
```

對應使用者階段（`derive_match_stage`）：

| match 狀態 | 使用者角色 | stage |
| :--- | :--- | :--- |
| draft | from_user | waiting_user（等發起者決定） |
| pending | from_user | waiting_other（等對方回覆） |
| pending | to_user | incoming_decision（對方想認識你） |
| accepted | 任意 | completed |

### 4.4 reconcile_match_state

每次配對相關請求前都會呼叫，做三件事：
1. 過期 draft（>24h）/ pending（>72h）。
2. 清除已失效的 `active_match_proposal_id`。
3. 清除超過 5 分鐘的 `matchmaking_in_progress` 鎖。

最後回傳「目前阻塞中的 match」—查詢條件為：
- `draft + from_user`（發起者自己還沒決定的草稿）
- `pending + to_user`（自己是接收者、等自己回覆的）

**這次修改的重點**：pending 不再因 `from_user` 阻塞發起者，讓發起者按「有興趣」後可立刻再找下一位。

### 4.5 媒人事件 queue（`mediator_event_service.py`）

阿月要對使用者說的話不是即時推播，而是寫進 `profiles.mediator_inbox`，由前端輪詢 `proactive_check` 領取。事件有優先序（`match_proposal=100` 最高），同優先序按時間排序。`claim_next_mediator_event` 用樂觀鎖避免多視窗重複領取。

### 4.6 記憶觀察（`memory_service.py`）

使用者在聊天中的第一人稱偏好會被萃取寫入 Neo4j：
- `observe_user_memory`：委派 9001 `/api/memory/observe`，LLM 抽取 confidence≥0.85 的偏好，寫 Graph 並更新 `profile_memory_preview` 與 `memory_notices`。
- `apply_memory_action`：disable/restore/correct 記憶。
- 9001 不可用時有直連 Neo4j 的 fallback。

---

## 5. Matchmaker Agent (9001) 詳細

### 5.1 端點

| 路徑 | Method | 職責 |
| :--- | :--- | :--- |
| `/api/match` | POST | 接收 target_user + candidates，讀雙方 graph_memory + 全域法則，呼叫 LLM 決策，回傳 matches[] |
| `/api/feedback` | POST | 收婉拒回饋，LLM 萃取 DISLIKES_TRAIT 寫入 Neo4j |
| `/api/global_reflection` | POST | 配對成功時歸納全域法則，寫入 GlobalRule |
| `/api/clear_graph` | POST | 清空整個 Neo4j |
| `/api/memory/observe` | POST | 從句子萃取偏好寫 Graph |
| `/api/memory/{user_id}` | GET | 列出使用者活躍記憶 |
| `/api/memory/action` | POST | disable/restore/correct 記憶 |

### 5.2 LLM 決策（`matchmaker.py`）

`MatchmakerAgent.system_prompt` 定義阿月人設與輸出格式（純 JSON `matches[]`）。關鍵設計：

- **角色錨定**：明確「你自己就是阿月」，對發起者用「你」、候選人用「他/她」，禁止用「嘿阿月」稱呼對方。
- **Graph 雙向檢查**：`[GRAPH_MEMORY_PLACEHOLDER]` 放發起者地雷；每位 candidate dict 自帶 `graph_memory` 欄位（候選人本人地雷），prompt 明確要求雙向比對、不可搞錯邊。
- **資料歸屬**：current_context / big_five / deep_profile / graph_memory 都綁定所屬 user，寫理由時不可張冠李戴。
- **輸出**：`recommendation_reason`（給發起者）+ `receiver_reason`（給接收者）雙視角，`distinctive_tags`（候選人動態標籤），`score_breakdown`，`top_reasons`。
- **決策鐵則**：DISLIKES_TRAIT 命中即不可推薦；加權評分：情境 30%、Graph 25%、深層價值觀 20%、Big Five 15%、立即可聊 10%。
- **防呆**：`agent_api.py` 對 JSON parse 失敗有 fallback（抓名單第一位、用 raw_response 當 reason）。

### 5.3 反思機制

- **個體反思**（`generate_graph_reflection`）：婉拒時從回饋萃取 DISLIKES_TRAIT 寫入 Neo4j。
- **全域反思**（`generate_global_reflection`）：配對成功時歸納抽象化通用法則，weight 疊加。
- Agent 內部用 `agent_memory_db` dict 暫存歷史，反思完清空。

---

## 6. 前端（`frontend.html`）

單一 HTML 檔，~2860 行，Tailwind CDN + 原生 JS，無框架。由 8000 的 `GET /` 直接回傳。

### 6.1 階段切換（`switchStage`）

```
big_five → deep_profile → messenger
```

- `big_five` / `deep_profile`：與阿月對話收斂性格與價值觀。
- `messenger`：通訊軟體介面，左側聯絡人清單、右側聊天室，`ai_assistant` 聯絡人即阿月。

### 6.2 配對進度卡（`refreshMatchStatus`）

當 `activeContactId === "ai_assistant"` 時顯示 `match-progress-card`，輪詢 `GET /api/match/status`：
- 狀態映射 `matchStageView`：idle / queued / searching / vector_search / graph_check / writing_reason / waiting_user / waiting_other / incoming_decision / completed 等。
- 進入 active stage（queued~writing_reason）時啟動 `setInterval(refreshMatchStatus, 1000)` 並 500ms 後補一次，狀態條即時更新。
- 同時帶回 `active_proposal_card`，可點擊 `openActiveProposalCard` 在聊天室渲染提案卡。

### 6.3 提案卡片渲染

阿月牽線提案卡（`appendRoomMessage` mediator_card 分支）目前精簡為：
- 標題 badge `[ 阿月牽線提案 ]`
- AI 產生的一段話總結（`recommendation_reason` 或 `receiver_reason`）
- 兩個按鈕：「有興趣，幫我問問」、「先不要，幫我婉拒」

已移除：近期情境對照、看中的點、相配度顯示。

### 6.4 輪詢機制

| 輪詢 | 週期 | 職責 |
| :--- | :--- | :--- |
| `checkNotifications` | 5s | `GET /api/notifications`（接收者 pending 邀請，delivery_channel≠mediator_chat）+ `GET /api/proactive_check`（阿月事件 queue） |
| `refreshMatchStatus` | 1s（active 時） | `GET /api/match/status`（配對進度） |

---

## 7. 關鍵流程時序

### 7.1 主動配對（「請阿月再找找」）

```
前端                      8000 Backend                     9001 Agent
 │                          │                                │
 ├─ POST /api/match/request │                                │
 │   confirmed=false ───────▶│                                │
 │                          │ reconcile → awaiting_confirm   │
 │ ◀── awaiting_confirmation│                                │
 │                          │                                │
 ├─ POST /api/match/request │                                │
 │   confirmed=true ───────▶│                                │
 │                          │ match_search=queued            │
 │                          │ BackgroundTask:                 │
 │                          │   create_proactive_match_proposal│
 │ ◀── queued ──────────────│                                │
 │                          │                                │
 │  (前端啟動 1s 輪詢)      │                                │
 │                          │ generate_matches_for_user:     │
 │                          │   vector search → top5         │
 │                          │   POST /api/match ────────────▶│ LLM 決策
 │                          │                                │ Graph 查詢
 │                          │ ◀── matches[] ──────────────── │
 │                          │ build_validated_explanation    │
 │                          │ insert match (draft)           │
 │                          │ queue_mediator_event            │
 │                          │   (match_proposal)             │
 │                          │                                │
 ├─ GET /api/proactive_check│                                │
 │ ◀── match_proposal event │                                │
 │  渲染提案卡片            │                                │
 │  [有興趣] [先不要]        │                                │
```

### 7.2 發起者按「有興趣」（draft → pending）

```
前端                      8000 Backend                     9001 Agent
 │                          │                                │
 ├─ POST /api/match/accept  │                                │
 │   match_id ──────────────▶│                                │
 │                          │ match: draft→pending            │
 │                          │ from_user: match_search=       │
 │                          │   waiting_other                │
 │                          │ to_user: active_match_proposal  │
 │                          │   =match_id,                   │
 │                          │   match_search=incoming_decision│
 │                          │ queue_mediator_event to to_user │
 │                          │   (incoming_match_interest)    │
 │                          │   含 receiver_reason ─────────▶│ (不直接呼叫)
 │ ◀── success, pending ───│                                │
 │                          │                                │
 │  (發起者可立刻再找下一位) │                                │
 │  因 reconcile 只查        │                                │
 │  pending+to_user         │                                │
```

### 7.3 接收者收到邀請

```
接收者前端                 8000 Backend
 │                          │
 ├─ GET /api/proactive_check (5s 輪詢)
 │ ◀── incoming_match_interest event
 │  渲染提案卡片（receiver_reason）
 │  [有興趣] [先不要]
 │
 ├─ POST /api/match/accept
 │   match_id ──────────────▶│ pending→accepted
 │                          │ 雙方 match_search=completed
 │                          │ BackgroundTask: generate_peer_first_message
 │                          │   (LLM 以發起者性格破冰)
 │                          │ BackgroundTask: global_reflection ──▶ 9001
 │ ◀── success, accepted ──│
 │  跳轉至聊天室
```

---

## 8. 設定與環境

### 8000 Backend (`social_demotest/.env`)

| 變數 | 用途 |
| :--- | :--- |
| `MONGO_URI` | MongoDB Atlas 連線字串 |
| `MONGO_DB_NAME` | 資料庫名稱（預設 `profiling_db`） |
| `GOOGLE_AI_STUDIO_API_KEY` / `GOOGLE_API_KEY` | Google embedding |
| `GOOGLE_EMBEDDING_MODEL` | embedding 模型（預設 `gemini-embedding-2`） |
| `OLLAMA_HOST` | Ollama 端點（預設 `https://ollama.com`） |
| `OLLAMA_API_KEY` | Ollama Cloud 金鑰 |
| `OLLAMA_CHAT_MODEL` | chat 模型（預設 `gemini-3-flash-preview:cloud`） |

### 9001 Agent (`matchmaker_agent/.env`)

| 變數 | 用途 |
| :--- | :--- |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID` | OpenAI 相容 LLM |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | Neo4j 連線 |

---

## 9. 觀察與注意事項

1. **雙 LLM 管線**：8000 用 Ollama Cloud（chat/embedding），9001 用 OpenAI 相容 client，兩條獨立，模型可不同。
2. **配對決策權在 9001**：8000 只做 vector search + 候選人預篩，最終唯一入選由 9001 LLM 決定。8000 的 `ai_service.match_candidates` 是舊版 fallback，目前管線走 `match.py:generate_matches_for_user` → 9001。
3. **`profiles` collection 職責重**：同時存放畫像、配對狀態機、事件 queue、記憶 preview，欄位多。
4. **事件非即時推播**：阿月訊息進 `mediator_inbox`，前端 5s 輪詢 `proactive_check` 領取，非 WebSocket。
5. **Graph 記憶有 fallback**：9001 不可用時，8000 `memory_service` 可直連 Neo4j 維持記憶觀察/動作，但配對決策（9001 LLM）無 fallback。
6. **vector search 依賴 Atlas**：`$vectorSearch` 需在 MongoDB Atlas 建立 `vector_index`，本地 MongoDB 不支援。
7. **TTL**：draft 24h、pending 72h、search lock 5min，由 `reconcile_match_state` 在每次相關請求時清理。

---

## 10. 本次修改的影響點

1. **`match.py:75-80`** `reconcile_match_state` 查詢改為 `pending + to_user` only — 發起者按「有興趣」後不再被自身 pending 阻塞。
2. **`frontend.html` 提案卡** 移除近期情境對照、看中的點、相配度，僅留一段話總結 + 兩按鈕。
3. **`match.py:716`** 接收者 matches item 補 `receiver_reason` 欄位 — 接收者端能看到完整原因。
4. **`frontend.html` 狀態條** 輪詢 2s→1s、確認後立即啟動 timer + 500ms 補查 — 搜尋中即時更新。
5. **`matchmaker.py` system_prompt** 角色錨定 + Graph 邊界說明 — 避免阿月被當成稱呼對象、避免資料搞錯邊。