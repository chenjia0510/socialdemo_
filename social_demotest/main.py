import sys
from fastapi import FastAPI
from routers import chat, match, system, frontend

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="Profiling System API", description="AI Matchmaker API Backend")

app.include_router(frontend.router)
app.include_router(chat.router)
app.include_router(match.router)
app.include_router(system.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
