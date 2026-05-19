import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from routes.bot_routes  import router as bot_router
from routes.ws_scan     import router as ws_router
from routes.auth_routes import router as auth_router
from trader.bot import run_loop
import os
from dotenv import load_dotenv
load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(run_loop())
    yield
    task.cancel()

app = FastAPI(title="Crypto Trading Bot", lifespan=lifespan)

# Allow the React UI to connect (adjust origins for production)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "ngrok-skip-browser-warning"],
)

app.include_router(bot_router)
app.include_router(ws_router)
app.include_router(auth_router)

@app.get("/")
def root():
    ui = os.path.join(os.path.dirname(__file__), "scanner-ui", "index.html")
    if os.path.exists(ui):
        return FileResponse(ui, media_type="text/html")
    return {"status": "running", "paper_trade": os.getenv("PAPER_TRADE","true"), "docs": "/docs"}
