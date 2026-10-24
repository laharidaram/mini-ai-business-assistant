"""FastAPI layer: thin HTTP wrapper around queries.ask()."""
import logging, os
from pathlib import Path
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from .db import AppError, connect, data_quality, load_csv
from .interpreter import MODEL, SUPPORTED, interpret, mode
from .queries import ask

BASE = Path(__file__).resolve().parent.parent
log = logging.getLogger("app")


class Ask(BaseModel):
    question: str


def create_app(csv_path=BASE / "data" / "sales.csv", db_path=None, interpret_fn=interpret):
    db_path = db_path or os.getenv("DB_PATH", BASE / "sales.db")
    loaded, rejected = load_csv(csv_path, db_path)          # fail fast at startup if the CSV is unusable
    log.info("Loaded %d rows, rejected %d", loaded, rejected)
    app = FastAPI(title="Mini AI Business Intelligence Assistant")

    @app.exception_handler(AppError)
    async def on_app_error(_, e):
        return JSONResponse({"status": "error", "message": e.message, "supported_questions": SUPPORTED}, e.status)

    @app.exception_handler(RequestValidationError)
    async def on_bad_request(_, e):
        return JSONResponse({"status": "error", "message": 'Send JSON like {"question": "..."}.',
                             "supported_questions": SUPPORTED}, 422)

    @app.exception_handler(Exception)
    async def on_unexpected(_, e):
        log.exception("Unhandled error")
        return JSONResponse({"status": "error", "message": "Something went wrong on our side."}, 500)

    @app.post("/ask")
    def post_ask(body: Ask):
        return ask(body.question, db_path, interpret_fn)

    @app.get("/api/mode")
    def get_mode():
        m = mode()
        return {"mode": m, "model": MODEL if m == "llm" else None}

    @app.get("/api/data-quality")
    def get_data_quality():
        conn = connect(db_path)
        try:
            return data_quality(conn)
        finally:
            conn.close()

    @app.get("/")
    def index():
        return FileResponse(BASE / "static" / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
