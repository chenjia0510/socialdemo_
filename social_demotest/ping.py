import requests
try:
    resp = requests.post("http://127.0.0.1:8000/api/chat", json={
        "user_id": "demo_user",
        "message": "hello",
        "state": "big_five"
    })
    print("Status code:", resp.status_code)
    print("Response payload:", resp.text)
except Exception as e:
    print("Requests exception:", e)
