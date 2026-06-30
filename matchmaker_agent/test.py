# test_run.py
import os
from dotenv import load_dotenv

# 這裡先寫最簡單的邏輯，確認套件有沒有抓到、金鑰有沒有讀到
load_dotenv()

api_key = os.getenv("LLM_API_KEY")
model = os.getenv("LLM_MODEL_ID")

if api_key:
    print("✅ 環境變數讀取成功！準備啟動 Nanobot...")
    print(f"🤖 使用模型: {model}")
    # 下一步我們就會把 Nanobot 的初始化程式碼寫在這裡
else:
    print("❌ 找不到 API Key，請檢查 .env 檔案！")