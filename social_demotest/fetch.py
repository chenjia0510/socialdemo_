import requests
import json
try:
    resp = requests.post("http://127.0.0.1:8000/api/chat", json={"user_id": "demo_user", "message": "hello", "state": "big_five"})
    with open("chat_resp.txt", "w", encoding="utf-8") as f:
        f.write(resp.text)
except Exception as e:
    with open("chat_resp.txt", "w", encoding="utf-8") as f:
        f.write(str(e))
