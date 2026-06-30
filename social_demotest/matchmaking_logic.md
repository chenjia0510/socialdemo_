# AI 媒人系統 - 配對邏輯與流程說明

這份文件記錄了目前後端所實作的「動態配對機制」與整體邏輯流轉，方便與前端/介面組員合併與核對串接細節。

---

## 邏輯架構與流程

整個媒合系統分為三大階段：「**資料建構**」、「**配對演算法 (雙層篩選)**」與「**互動流轉**」。

### 階段一：資料建構 (Profiling & Context)

為了讓配對具有「性格互補」與「當下情境契合」兩個維度，我們在對話階段會分別搜集：

1. **性格分析 (Big Five)**
   - 使用者一登入時（`stage-bigfive`），AI 助理會根據對話（`POST /api/chat`）動態收斂使用者的「大五人格模型」（開放性 O、盡責性 C、外向性 E、親和性 A、神經質 N）。
   - 當收集超過 3 輪並達到滿意水準時，會將 JSON 格式的性格資料寫入資料庫 `profiles_coll`。

2. **近期情境 (Current Context)**
   - 進入通訊軟體 (`stage-messenger`) 後，當使用者與「AI 小助手」聊天提到近況（例如：「我想去喝咖啡」或「最近壓力很大」），後端 `POST /api/direct_chat` 路由中，會自動擷取該訊息，並透過 **Google AI Studio 的 `models/text-embedding-004`** 將其轉換為向量 (Embedding) 儲存下來。

### 階段二：雙層篩選配對演算法 (The Matchmaking algorithm)

當使用者點擊「🌟 尋找配對」觸發 `POST /api/match` 時，後端會執行極具效率的**雙重篩選**：

1. **第一層篩選：MongoDB Atlas Vector Search**
   - 由於資料庫可能會有成千上萬筆資料，直接抓到後端記憶體計算或餵給 LLM 判斷是不切實際的。
   - 因此系統會利用 MongoDB Atlas 內建的 `$vectorSearch` 功能，直接在資料庫層級比對 發起人 (User A) 的 `context_embedding` 與所有目標使用者的向量相似度。
   - 資料庫會自動過濾掉已配對過的人，並直接回傳「分數最高的前 5 名候選人」。這保證了這 5 個人在「近況或想做的事情」上非常有共鳴（例如，A 想打球，系統會先把同樣想打球或想要運動的人挑出來），且大幅減少後端伺服器的運算負載。

2. **第二層篩選：LLM 情感智慧裁決**
   - 系統將 User A 的性格與情境，連同這前 5 名候選人的性格與情境打包成 Prompt 送給大型語言模型（由 `OLLAMA_CHAT_MODEL` 指定，現為 `gemini-3-flash-preview:cloud`）。
   - LLM 會扮演專業媒人，除了情境之外，還會評估「**大五人格是否互補、溝通是否合拍**」，最終從 5 人中唯一指定 **1 位最佳配對對象 (User B)**，並給出一份**感性且白話的配對理由**（"reason"）。

### 階段三：邀請通知與破冰系統

1. **狀態建立**
   - 系統將配對結果寫入 `matches_coll` (邀請狀態設為 `pending`)。發起方 (User A) 畫面會跳出等待中的彈窗。
2. **目標用戶輪詢**
   - 目標對象 (User B) 登入期間，前端會不斷向 `GET /api/notifications` 發送輪詢。
   - 抓取到 `pending` 狀態的邀請後，User B 會看到彈窗通知，內容包含 AI 生成的「配對理由」。
3. **同意與 AI 代理搭訕 (Surrogate Initialization)**
   - 若 User B 點擊同意 (`POST /api/match/accept`)，邀請轉為 `accepted`。
   - **關鍵亮點**：為了不讓新建立的聊天室空蕩蕩，後端會在同意的當下，**立刻請 LLM 讀取 User A 的大五人格設定與配對理由，以 User A 的性格「代理」他向 User B 發送第一句搭訕/破冰訊息**。
   - 雙方此後可透過各自的聊天框針對此紀錄進行互動 (`POST /api/direct_chat`)。

---

## 相關 API 整理

| API 路徑 | Method | 負責功能 / 給前端的用途 |
| :--- | :--- | :--- |
| `/api/chat` | POST | 進行大五人格測驗的互動收斂。 |
| `/api/direct_chat` | POST | 進行正式 Messenger 的 1-on-1 對話。如果是針對 `ai_assistant`，會順便更新 `context_embedding`。如果是配對用戶，讀取對方大五人格進行角色扮演。 |
| `/api/match` | POST | 發起媒合，觸發雙層演算法，回傳配對結果與理由。 |
| `/api/notifications` | GET | 讓前端輪詢，檢查是否有別人丟過來的 `pending` 邀請。 |
| `/api/match/accept` | POST | 接受好友邀請，並自動讓對方拋出破冰問候。 |
| `/api/contacts` | GET | 抓取好友名單（狀態為 `accepted` 的雙邊用戶）。 |

---
這份文件主要說明後端的邏輯架構，任何進階的前端介面調整（如彈窗動畫、CSS 樣式）可由您的前端組員直接在此基礎上進行加工。如果有需要新增特定狀態碼或資料欄位，隨時可以再做調整！
