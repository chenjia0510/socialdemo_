# 私訊阿月邏輯

> 對應程式碼：`social_demotest/routers/chat.py`（`/api/mediator/private`、`/api/mediator/probe`、`/api/mediator/private/{other_id}`）

---

## 1. 什麼是私訊阿月

當兩人配對成功（match.status = accepted）後，每位使用者可以在**自己的私人聊天室**裡跟阿月聊關於配對對象的事。這個聊天室只有「使用者本人 + 阿月」看得到，對方看不到。

**room_id 格式**：`mediator_private::{user_id}::{other_id}`

**入口端點**：

| 端點 | Method | 職責 |
| :--- | :--- | :--- |
| `/api/mediator/private/{other_id}` | GET | 載入私訊歷史、未讀數、pending 狀態 |
| `/api/mediator/private` | POST | 送出訊息給阿月，阿月回覆 |
| `/api/mediator/probe` | POST | 主動請阿月去探對方口風 |

**門檻**：必須有 accepted match，否則 403「只有已配對的聊天室可以私訊阿月」。

---

## 2. 私訊阿月能做的事

私訊阿月是一個**多階段狀態機**，使用者說的話會被路由到不同分支：

```
使用者送訊息
    │
    ├─ 有 pending_date_coordination？
    │   ├─ 是取消約會？ ──▶ 取消約會協調
    │   ├─ 說「協調約會」？ ──▶ 啟動約會協調流程
    │   └─ 在約會協調流程中？ ──▶ 下一階段問題
    │
    ├─ 有 pending_private_feedback？
    │   ├─ stage=probe_answer ──▶ 收低敏感探測答案
    │   ├─ stage=sentiment + 探口風指令 ──▶ 發起 sentiment 探測
    │   ├─ stage=sentiment ──▶ 分類情緒 → 進入 consent
    │   └─ stage=consent ──▶ 處理同意/保密
    │
    ├─ 使用者主動說探口風？
    │   └─ request_relationship_probe
    │
    └─ 一般私訊
        └─ LLM 帶關係 context 回答
```

---

## 3. 約會協調（date coordination）

使用者說「協調約會」即可啟動。阿月會一步步問，只帶回交集。

### 3.1 流程

```
stage: availability ──▶ activity ──▶ budget ──▶ 完成
   │                    │             │
   │                    │             └─ 預算：500內 / 500到1000 / 1000以上
   │                    └─ 活動：咖啡 / 散步 / 電影 / 吃飯 / 展覽
   └─ 時段：平日晚上 / 週末早上 / 週末下午 / 週末晚上
```

### 3.2 完成時

- 兩人都填完 → 算 `date_overlap`（time + activity + budget 交集）
- 有交集：「有交集：平日晚上去咖啡，預算建議 500內」
- 無交集：「目前時間或活動還沒交集，我先不硬湊」
- 結果透過 `date_coordination_result` 事件推給對方

### 3.3 取消

使用者說「不要了/取消/先不用」→ `date_coordination.status=cancelled`，通知對方「這次約會協調先暫停」。

### 3.4 資料儲存

- 進行中：`profiles.pending_date_coordination`（per-user）
- 完成：`matches.date_coordination.participants.{role}` + `overlap` + `status`

---

## 4. 探口風（probe）

使用者可以請阿月去對方那邊「探口風」，了解對方對這段關係的感覺。

### 4.1 觸發方式

- **主動**：使用者在私訊說「喜歡我嗎/有好感/怎麼看我/探口風/幫我問」
- **手動**：`POST /api/mediator/probe`
- **自動**：`queue_due_feedback`（見下節）

### 4.2 probe 類型（PROBE_QUESTIONS）

| kind | 問題 | 敏感度 |
| :--- | :--- | :--- |
| `sentiment` | 「剛剛聊起來感覺怎樣？」 | 高 |
| `fun_fact` | 「說一個最近發生、會讓人更認識你的有趣小事？」 | 低 |
| `weekend` | 「你最近理想的週末長什麼樣？」 | 低 |
| `conversation_hook` | 「最近有哪個話題一聊就停不下來？」 | 低 |
| `availability` | 「最近如果有人約你出去，你偏好平日晚上還是週末？」 | 低 |

`choose_probe_kind` 會避免短期內重複同一類型。

### 4.3 探測狀態機（被探測方）

```
queued ──(前端領取)──▶ awaiting_answer (低敏感)
                     或 awaiting_sentiment (sentiment)

awaiting_answer ──(回答)──▶ completed
  └─ 只帶回 60 字摘要給請求者（probe_result 事件）

awaiting_sentiment ──(回答情緒)──▶ awaiting_consent
  └─ classify_feedback：positive / negative / neutral

awaiting_consent ──(同意/保密)──▶ completed
  ├─ 同意：deliver_consented_signal → probe_result 給請求者
  └─ 保密：不轉告
```

### 4.4 雙方好感對撞（mutual_interest）

若雙方都透過 probe 回饋 `sentiment=positive` + `share_consent=true`：

```
queue_mediator_event(user, "偷偷跟你說，X 對你的印象也超級好...", "mutual_interest")
queue_mediator_event(other, "偷偷跟你說，Y 對你的印象也超級好...", "mutual_interest")
```

### 4.5 婉拒轉告（gentle_closure）

若被探測方 `sentiment=negative` + `share_consent=true`：

```
queue_mediator_event(請求者, "我幫你探過口風了，你們目前節奏沒對上...", "gentle_closure")
```

### 4.6 冷卻機制

- `probe_policy`：依 `probe_mode` 設定
  - `manual`：不自動探測
  - `active`：min_messages=6, idle=600s, cooldown=21600s（6h）
  - `balanced`（預設）：min_messages=8, idle=1800s（30m）, cooldown=86400s（24h）
  - DEMO 快速模式（`MEDIATOR_DEMO_FAST_PROBE=1`）：min_messages=6, idle=120s, cooldown=300s
- `PROBE_PENDING_TTL`：72h，超過未回覆的 probe 自動 expired
- 完成後需對方再聊 6 則新訊息才能再探

---

## 5. 自動探口風（queue_due_feedback）

`chat.py:503`，在 `proactive_check` 每次輪詢時呼叫：

**條件**（全部滿足才自動探測）：
1. `probe_mode != manual`
2. 有 accepted match
3. `shared_message_count >= min_messages`（訊息數達標）
4. `last_chat_at < now - idle_seconds`（閒置夠久）
5. 目前沒有 in-flight probe
6. 距上次完成有 6 則新訊息以上且過了 cooldown

觸發後 `queue_mediator_event(probe_question)`，被探測方下次輪詢會收到。

---

## 6. 回饋收集（feedback）

### 6.1 自動回饋請求

配對成功且聊了幾句後，阿月會主動問「剛剛聊起來感覺怎樣？」（`feedback_request` 事件）。

### 6.2 回饋處理（handle_private_feedback）

`chat.py:347`：

1. `classify_feedback`：先用關鍵字判斷 positive/negative/neutral，不明確則 LLM 判斷
2. 偵測 `share_consent`：使用者說「可以透露/可以跟他說/願意分享」
3. 寫入 `match.private_feedback.{user_id}`
4. 若對方曾請求探口風且同意分享 → `probe_result` 通知對方
5. 若雙方都 positive + consent → `mutual_interest` 雙方都通知
6. 若 negative + consent → `gentle_closure` 通知對方

### 6.3 隱私保護

- 不同意分享 → 「這段話只留在我們之間」
- 同意分享 → 只說「大方向」，不搬原話
- `safe_summary`：低敏感 probe 只帶回 60 字摘要

---

## 7. 一般私訊（非指令）

當使用者沒有 pending 狀態、也不是探口風指令時，走**一般 LLM 回覆**（`chat.py:1504`）：

### 7.1 context 組裝

```python
relationship_context = {
    "viewer": mediator_profile_context(user_id, message),
        # owner_user_id, initial_interest, current_context,
        # big_five, deep_profile, graph_memories (relevant)
    "partner": mediator_profile_context(other_id, message),
        # 同上，但是對方的
    "relationship": {
        "match_id", "match_reason_for_viewer",
        "validated_reason_items",  # 配對時的 reason_items
        "shared_chat_summary",     # relationship_memory
        "latest_shared_chat",      # 最近 16 則雙方聊天
        "viewer_mediator_state",   # 自己的 probe 狀態
        "partner_mediator_state",  # 對方的 probe 狀態
        "partner_consented_signal" # 對方同意透露的 sentiment
    }
}
```

### 7.2 prompt 重點

```
{MEDIATOR_PERSONA}
你的說話風格：{mediator_style}
你是全域聊天室與悄悄話中同一位阿月。
兩人的性格、情境、偏好都不可交換歸屬。
每個事實只能屬於它的 owner_user_id。
請積極運用 partner 的 graph_memories、big_five 等資料回答。
可以大方透露 partner 的情報（喜好特質、習慣、近期情境）促進認識。
不知道就直接說不知道，不引用私人原話、不補故事。
```

### 7.3 relevant_graph_memories

`chat.py:126`，會依使用者訊息做 bigram 相關性排序，挑出與問題最相關的 graph 記憶（最多 9 條）。

---

## 8. 關係記憶摘要（summarize_relationship）

`chat.py:476`，配對聊天室有新訊息時背景觸發：

- 條件：訊息數 ≥6 且距上次摘要新增 ≥4
- LLM 讀最近 20 則聊天
- 產生：`shared_summary`、`interaction_tone`、`common_topics`、`conversation_hooks`
- 存入 `match.relationship_memory`
- 供私訊阿月回答時引用

---

## 9. 未讀數管理

- `match.private_unread.{from|to}`：私訊阿月的未讀數
- 進入私訊面板時歸零（`get_mediator_private_messages`）
- 阿月事件推播時 +1（`proactive_check` delivery 時 `$inc`）

---

## 10. 私訊阿月的邊界

| 可以 | 不可以 |
| :--- | :--- |
| 透露對方同意分享的情緒大方向 | 搬運對方的原話 |
| 透露對方的 graph_memories、big_five、current_context | 替對方補故事或捏造 |
| 主動探口風、約會協調、收回饋 | 在對方沒同意時轉告負面回饋 |
| 帶回雙方交集的約會資訊 | 硬湊沒有交集的行程 |
| 說「我不知道」 | 假裝知道對方沒說過的事 |