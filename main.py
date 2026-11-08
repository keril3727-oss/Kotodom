import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "kotodom.db")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = FastAPI(title="Kotodom", description="Помощь бездомным котятам и животным")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with closing(db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS animals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                description TEXT NOT NULL,
                photo_url TEXT DEFAULT '',
                contact TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Ищет дом',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                animal_id INTEGER NOT NULL,
                applicant_name TEXT NOT NULL,
                contact TEXT NOT NULL,
                message TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(animal_id) REFERENCES animals(id)
            )
        """)
        con.commit()


@app.on_event("startup")
def startup():
    init_db()


class AnimalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    city: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=5, max_length=1500)
    photo_url: str = Field(default="", max_length=500)
    contact: str = Field(min_length=3, max_length=150)


class ApplicationCreate(BaseModel):
    animal_id: int
    applicant_name: str = Field(min_length=1, max_length=100)
    contact: str = Field(min_length=3, max_length=150)
    message: str = Field(default="", max_length=1000)


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "project": "kotodom"}


@app.get("/api/animals")
def list_animals(city: Optional[str] = None):
    with closing(db()) as con:
        if city:
            rows = con.execute(
                "SELECT * FROM animals WHERE city LIKE ? ORDER BY id DESC",
                (f"%{city}%",)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM animals ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/animals")
def create_animal(animal: AnimalCreate):
    with closing(db()) as con:
        cursor = con.execute(
            """INSERT INTO animals (name, city, description, photo_url, contact)
               VALUES (?, ?, ?, ?, ?)""",
            (animal.name, animal.city, animal.description, animal.photo_url, animal.contact)
        )
        con.commit()
        animal_id = cursor.lastrowid
    return {"ok": True, "id": animal_id, "message": "Объявление отправлено"}


@app.post("/api/applications")
def create_application(application: ApplicationCreate):
    with closing(db()) as con:
        exists = con.execute(
            "SELECT id FROM animals WHERE id = ? AND status = 'Ищет дом'",
            (application.animal_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Объявление не найдено")
        con.execute(
            """INSERT INTO applications (animal_id, applicant_name, contact, message)
               VALUES (?, ?, ?, ?)""",
            (application.animal_id, application.applicant_name,
             application.contact, application.message)
        )
        con.commit()
    return {"ok": True, "message": "Заявка отправлена. Спасибо за желание помочь!"}


async def send_telegram_message(chat_id: int, text: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if chat_id and text:
        if text.startswith("/start"):
            reply = (
                "Привет! Это КОТОДОМ 🐾\n\n"
                "Здесь мы помогаем котятам и другим животным найти дом.\n"
                "Сайт с объявлениями скоро откроется.\n\n"
                "Команды:\n"
                "/help — помощь"
            )
        elif text.startswith("/help"):
            reply = "КОТОДОМ помогает искать дом животным и находить волонтёров. Скоро здесь появятся новые функции."
        else:
            reply = "Спасибо, что хотите помочь! Напишите /help, чтобы узнать о проекте."
        await send_telegram_message(chat_id, reply)

    return {"ok": True}
