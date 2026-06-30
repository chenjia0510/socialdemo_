import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Frontend"])

@router.get("/", response_class=HTMLResponse)
def serve_frontend():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(base_dir, "frontend.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()
