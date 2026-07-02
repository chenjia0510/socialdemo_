# 配對邏輯

> 對應程式碼：`social_demotest/routers/match.py`、`matchmaker_agent/`、`social_demotest/services/ai_service.py`

---

## 1. 觸發方式

系統有兩種觸發配對的路徑：

### 1.1 主動配對（聊天中自動觸發）

**位置**：`chat.py:1005`，在使用者與阿月（`ai_assistant`）聊近況時觸發。

**門檻條件（全部必須滿足）**：

| 條件 | 說明 |
| :--- | :--- |
| `context_changed` | LLM 判定情境有更新（`context_should_update=true` 且 `context_confidence≥0.85`） |
| `readiness_score >= 75` | `MATCH_READINESS_THRESHOLD`，由 `deterministic_readiness` 計算 |
| `not active_match` | 沒有進行中的 draft/pending 配對 |
| `last_auto_revision != new_revision` | 這個情境版本還沒配過 |
| `not matching_in_progress` | 沒有正在跑的配對任務 |

**readiness_score 計算**（`chat.py:172 deterministic_readiness`）：

只有當對話意圖被 LLM 判定為 `recent_context` 才計分，四個訊號加總（滿分 100）：

| 訊號 | 權重 | 滿分條件 |
| :--- | :--- | :--- |
| `activity`（想做什麼） | 40 | 有值 |
| `timing`（時間感） | 20 | 有值 |
| `preference`（具體偏好） | 20 | 有值 |
| `companion_intent`（想找人同行） | 20 | 有值且非「不想/不要/自己去/不需要」 |

≥75 分代表至少要有 activity + 兩個其他訊號。觸發後**不直接配對**，而是設 `match_search=awaiting_confirmation`，阿月會問「要我開始翻名單嗎？」，使用者確認後才跑。

### 1.2 手動配對（「請阿月再找找」按鈕）

**位置**：`match.py:598 request_next_match`

**門檻**：無 readiness_score 檢查。只要 `reconcile_match_state` 回傳沒有阻塞中的配對即可。

流程：`awaiting_confirmation` → 使用者點確認 → `queued` → BackgroundTask 跑 `create_proactive_match_proposal` → `generate_matches_for_user`。

---

## 2. 配對管線（`generate_matches_for_user`）

```
1. 載入 user profile + 確保 context_embedding
   ├─ 若無 embedding，用 get_embedding(current_context) 產生
   └─ current_context 預設 "交朋友"

2. 組 excluded_users（排除清單）
   ├─ 排除自己
   ├─ accepted/draft/pending：全部排除
   └─ declined：30 天內或同 context_revision 排除

3. MongoDB Atlas $vectorSearch
   ├─ 用 context_embedding 做向量相似度搜尋
   ├─ numCandidates=50, limit=20
   ├─ $match 排除 excluded_users
   └─ 取 top 5 候選人

4. 補上每位候選人的 deep_profile
   └─ 從 profiles_coll 額外讀取

5. 打包 payload，POST http://127.0.0.1:9001/api/match
   ├─ target_user（發起者，已 strip context_embedding）
   ├─ candidates（每位已 strip，9001 會補 graph_memory）
   └─ target_deep_profile

6. 9001 Agent 回傳 matches[]
   ├─ matched_user_id
   ├─ recommendation_reason（給發起者的一段話）
   ├─ receiver_reason（給接收者的一段話）
   ├─ distinctive_tags（候選人動態標籤 5-6 個）
   ├─ score_breakdown（context/graph/values/personality/conversation/total）
   └─ top_reasons（三個具體理由）

7. build_validated_match_explanation（雙向，8000 自己算）
   ├─ target→candidate：產 recommendation 的 reason_items + score_breakdown
   ├─ candidate→target：產 receiver 的 reason_items + score_breakdown
   ├─ 查雙方 Graph 記憶（get_user_graph_memories）
   ├─ 算 shared_traits / conflicts / shared_values / shared_context / Big Five 距離
   └─ 產生 owner-bound evidence_ids

8. 組合最終原因
   ├─ ai_recommendation_reason = agent回傳 or 8000算的
   └─ ai_receiver_reason = agent回傳 or 8000算的

9. 寫入 matches_coll
   ├─ status = "draft"
   ├─ delivery_channel = "mediator_chat"
   ├─ match_context_snapshot（雙方 current_context + context_signals）
   └─ created_at

10. queue_mediator_event(match_proposal)
    └─ 進入 profiles.mediator_inbox，前端輪詢領取
```

---

## 3. 配對狀態機

```
                ┌─────────────────────────────────────────┐
                │                                         │
  draft  ──(發起者 accept)──▶ pending  ──(接收者 accept)──▶ accepted
   │                          │
   └──(發起者 decline)──▶ declined   └──(接收者 decline)──▶ declined

  draft > 24h ──▶ expired (draft_timeout)
  pending > 72h ──▶ expired (pending_timeout)
```

對應使用者階段（`derive_match_stage`）：

| match.status | 使用者角色 | stage | 意義 |
| :--- | :--- | :--- | :--- |
| draft | from_user | waiting_user | 等發起者決定 |
| pending | from_user | waiting_other | 等對方回覆 |
| pending | to_user | incoming_decision | 有人想認識你 |
| accepted | 任意 | completed | 配對完成 |

---

## 4. reconcile_match_state（每次配對請求前必跑）

`match.py:29`，做三件事：

1. **過期清理**：draft > 24h、pending > 72h → expired。
2. **清除失效的 active_match_proposal_id**：若指向的 match 已非 draft/pending，則 unset。
3. **清除過期的 search lock**：`matchmaking_in_progress` 超過 5 分鐘則釋放。

最後回傳「目前阻塞中的 match」—查詢條件：
- `draft + from_user`（自己還沒決定的草稿）
- `pending + to_user`（自己是接收者、等自己回覆的）

**關鍵**：pending 不因 `from_user` 阻塞發起者，讓發起者按「有興趣」後可立刻再找下一位。

---

## 5. 排除規則（excluded_users）

| 狀態 | 排除規則 |
| :--- | :--- |
| accepted | 永久排除 |
| draft | 排除 |
| pending | 排除 |
| declined | 30 天內排除，或同 context_revision 排除 |
| 自己 | 排除 |

---

## 6. 評分權重（LLM 決策鐵則）

9001 Agent 的 system_prompt 定義加權：

| 面向 | 權重 |
| :--- | :--- |
| 近期情境 (context) | 30% |
| 雙方 Graph (graph) | 25% |
| 深層價值觀 (values) | 20% |
| Big Five (personality) | 15% |
| 立即可聊話題 (conversation) | 10% |

**最高扣分項**：DISLIKES_TRAIT 命中對方特質 → 絕對不推薦。

---

## 7. 破冰訊息

配對成功（pending→accepted）時，`match.py:746` BackgroundTask：

```
generate_peer_first_message(initiator_doc, target_doc, reason)
└─ LLM 以「發起者性格」對接收者發送第一句搭訕
   ├─ 讀發起者 big_five
   ├─ 讀配對原因 reason
   ├─ 讀接收者 current_context
   └─ 存入 messages_coll（room_id = sorted([from, to])）
```

---

## 8. 全域反思

配對成功時同時觸發（`match.py:759`）：

```
POST 9001/api/global_reflection
├─ from_big_five, from_context
├─ to_big_five, to_context
└─ 9001 LLM 歸納抽象化通用法則
   ├─ 寫入 Neo4j GlobalRule 節點
   └─ weight 疊加（ON MATCH +1）
```

下次配對時 9001 會讀 top 3 高權重法則放進 prompt。