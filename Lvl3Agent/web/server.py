import os
import sys
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

THIS_DIR = os.path.dirname(os.path.abspath(__file__))        # .../bank_agent_app/web
ROOT_DIR = os.path.dirname(THIS_DIR)                         # .../bank_agent_app

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from agent_core.agent import run_agent_turn_async  # <--- still valid

app = FastAPI()

STATIC_DIR = os.path.join(THIS_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class AskBody(BaseModel):
    message: str

@app.post("/ask")
async def ask_llm(body: AskBody):
    user_msg = body.message.strip()
    if not user_msg:
        return JSONResponse({"answer": "(no input)"})

    try:
        answer = await run_agent_turn_async(user_msg)
        return JSONResponse({"answer": answer})

    except Exception as e:
        # DEBUG LOGGING: print the traceback so we can see why we're 500'ing
        import traceback
        print("=== BACKEND ERROR IN /ask ===")
        print(f"Exception type: {type(e).__name__}")
        print(f"Exception message: {e}")
        traceback.print_exc()
        print("=== END BACKEND ERROR ===")

        return JSONResponse(
            {"answer": f"[backend error] {type(e).__name__}: {e}"},
            status_code=500
        )




@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
