import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "kotodom.db")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", os.getenv("RENDER_EXTERNAL_URL", "https://kotodom.onrender.com")).rstrip("/")

app = FastAPI(title="КОТОДОМ", description="Адресная помощь животным")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                animal_name TEXT NOT NULL,
                city TEXT NOT NULL,
                country TEXT NOT NULL,
                story TEXT NOT NULL,
                need_type TEXT NOT NULL,
                clinic TEXT DEFAULT '',
                target_amount REAL NOT NULL,
                currency TEXT NOT NULL,
                evidence TEXT DEFAULT '',
                photo_url TEXT DEFAULT '',
                contact TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                raised_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bot_drafts (
                chat_id INTEGER PRIMARY KEY,
                step INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()


@app.on_event("startup")
async def startup():
    init_db()
    public_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if BOT_TOKEN and public_url:
        payload = {"url": public_url + "/telegram/webhook"}
        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", json=payload)
                r.raise_for_status()
                menu = {"menu_button": {"type": "web_app", "text": "Открыть КОТОДОМ", "web_app": {"url": WEBAPP_URL}}}
                m = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton", json=menu)
                m.raise_for_status()
        except Exception as exc:
            print(f"Telegram setup failed: {exc}")


class CaseCreate(BaseModel):
    animal_name: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=100)
    country: str = Field(pattern="^(Россия|Беларусь)$")
    story: str = Field(min_length=20, max_length=3000)
    need_type: str = Field(pattern="^(Лечение|Корм|Лечение и корм|Другое)$")
    clinic: str = Field(default="", max_length=180)
    target_amount: float = Field(gt=0, le=10000000)
    currency: str = Field(pattern="^(RUB|BYN)$")
    evidence: str = Field(default="", max_length=500)
    photo_url: str = Field(default="", max_length=500)
    contact: str = Field(min_length=3, max_length=150)


def case_dict(row):
    item = dict(row)
    item["remaining_amount"] = max(0, round(item["target_amount"] - item["raised_amount"], 2))
    return item


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "project": "kotodom"}


@app.get("/api/cases")
def list_cases():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT * FROM cases WHERE status='published' ORDER BY id DESC"
        ).fetchall()
    return [case_dict(row) for row in rows]


@app.get("/api/cases/{case_id}")
def get_case(case_id: int):
    with closing(db()) as con:
        row = con.execute(
            "SELECT * FROM cases WHERE id=? AND status='published'", (case_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Сбор не найден")
    return case_dict(row)


@app.post("/api/cases")
async def create_case(case: CaseCreate):
    with closing(db()) as con:
        cur = con.execute("""
            INSERT INTO cases
            (animal_name, city, country, story, need_type, clinic, target_amount,
             currency, evidence, photo_url, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (case.animal_name, case.city, case.country, case.story, case.need_type,
              case.clinic, case.target_amount, case.currency, case.evidence,
              case.photo_url, case.contact))
        con.commit()
        case_id = cur.lastrowid
    await notify_admin(
        "Новая заявка КОТОДОМ №{}\nЖивотное: {}\nНужда: {}\nГород: {}, {}\nСумма: {} {}\nКонтакт: {}\nИстория: {}".format(
            case_id, case.animal_name, case.need_type, case.city, case.country,
            case.target_amount, case.currency, case.contact, case.story[:700]
        )
    )
    return {"ok": True, "id": case_id, "status": "pending",
            "message": "Заявка принята на проверку. Сбор появится после подтверждения документов."}


def require_admin(key: Optional[str]):
    if not ADMIN_API_KEY or key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Нет доступа администратора")


@app.get("/api/admin/cases")
def admin_cases(x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM cases ORDER BY id DESC").fetchall()
    return [case_dict(row) for row in rows]


@app.post("/api/admin/cases/{case_id}/publish")
def publish_case(case_id: int, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    with closing(db()) as con:
        cur = con.execute("UPDATE cases SET status='published' WHERE id=?", (case_id,))
        con.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {"ok": True, "status": "published"}


@app.post("/api/admin/cases/{case_id}/reject")
def reject_case(case_id: int, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    with closing(db()) as con:
        cur = con.execute("UPDATE cases SET status='rejected' WHERE id=?", (case_id,))
        con.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {"ok": True, "status": "rejected"}


async def telegram_send(chat_id: str, message: str):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message}
        )


async def telegram_send_with_keyboard(chat_id: str, message: str, keyboard: dict):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "reply_markup": keyboard}
        )
        response.raise_for_status()


async def notify_admin(message: str, case_id: int):
    if not ADMIN_CHAT_ID or not BOT_TOKEN:
        return
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Одобрить и опубликовать", "callback_data": f"case:publish:{case_id}"},
            {"text": "❌ Отклонить", "callback_data": f"case:reject:{case_id}"}
        ]]
    }
    await telegram_send_with_keyboard(ADMIN_CHAT_ID, message, keyboard)


async def telegram_answer_callback(callback_id: str, text: str):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text, "show_alert": True}
        )
        response.raise_for_status()


async def telegram_edit_message(chat_id: str, message_id: int, text: str):
    if not BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        response.raise_for_status()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()

    callback = update.get("callback_query")
    if callback:
        callback_id = callback.get("id", "")
        callback_data = callback.get("data", "")
        callback_message = callback.get("message", {})
        callback_chat_id = str(callback_message.get("chat", {}).get("id", ""))
        message_id = callback_message.get("message_id")
        if callback_chat_id != str(ADMIN_CHAT_ID):
            await telegram_answer_callback(callback_id, "Нет доступа.")
            return {"ok": True}
        parts = callback_data.split(":")
        if len(parts) == 3 and parts[0] == "case" and parts[1] in ("publish", "reject") and parts[2].isdigit():
            case_id = int(parts[2])
            new_status = "published" if parts[1] == "publish" else "rejected"
            with closing(db()) as con:
                row = con.execute("SELECT animal_name,status FROM cases WHERE id=?", (case_id,)).fetchone()
                if not row:
                    await telegram_answer_callback(callback_id, "Заявка не найдена.")
                    return {"ok": True}
                if row["status"] != "pending":
                    await telegram_answer_callback(callback_id, "Эта заявка уже обработана.")
                    return {"ok": True}
                con.execute("UPDATE cases SET status=? WHERE id=?", (new_status, case_id))
                con.commit()
            result_text = "опубликована на сайте" if new_status == "published" else "отклонена"
            await telegram_answer_callback(callback_id, f"Заявка №{case_id} {result_text}.")
            await telegram_edit_message(
                callback_chat_id, message_id,
                f"Заявка №{case_id} — {row['animal_name']}\\nСтатус: {result_text}."
            )
        else:
            await telegram_answer_callback(callback_id, "Неизвестное действие.")
        return {"ok": True}

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    incoming = (message.get("text") or "").strip()
    if not chat_id or not incoming:
        return {"ok": True}

    if incoming in ("/myid", "/id"):
        await telegram_send(str(chat_id), f"Ваш Telegram ID для уведомлений КОТОДОМ: {chat_id}")
        return {"ok": True}

    if incoming in ("/cancel", "Отмена"):
        with closing(db()) as con:
            con.execute("DELETE FROM bot_drafts WHERE chat_id=?", (chat_id,))
            con.commit()
        await telegram_send(str(chat_id), "Заявка отменена. Если понадобится помощь, напишите /start.")
        return {"ok": True}

    if incoming.startswith("/start") or incoming.startswith("/help"):
        keyboard = {"inline_keyboard": [[{"text": "🐾 Открыть КОТОДОМ", "web_app": {"url": WEBAPP_URL}}]]}
        await telegram_send_with_keyboard(str(chat_id),
            "Здравствуйте! Это КОТОДОМ 🐾\\nОткройте мини-приложение, чтобы посмотреть сборы, пополнить общую копилку или отправить заявку.\\n\\n"
            "Если удобнее, заявку можно заполнить прямо в чате командой /apply.", keyboard)
        return {"ok": True}

    if incoming.startswith("/apply"):
        with closing(db()) as con:
            con.execute(
                "INSERT OR REPLACE INTO bot_drafts(chat_id,step,payload) VALUES(?,0,'{}')",
                (chat_id,)
            )
            con.commit()
        await telegram_send(str(chat_id),
            "Давайте оформим заявку на помощь 🐾\\nКак зовут животное? (Для отмены напишите /cancel)")
        return {"ok": True}

    with closing(db()) as con:
        draft = con.execute("SELECT step,payload FROM bot_drafts WHERE chat_id=?", (chat_id,)).fetchone()
        if not draft:
            await telegram_send(str(chat_id), "Чтобы подать заявку, напишите /start.")
            return {"ok": True}
        import json
        payload = json.loads(draft["payload"] or "{}")
        step = draft["step"]
        fields = [
            ("animal_name", "В каком городе находится животное?"),
            ("city", "Выберите страну: Россия или Беларусь."),
            ("country", "Опишите историю и проблему животного (не менее пары предложений)."),
            ("story", "Какая помощь нужна? Напишите: Лечение, Корм, Лечение и корм или Другое."),
            ("need_type", "Название клиники или поставщика корма (если известно). Если пока неизвестно, напишите «Не знаю»."),
            ("clinic", "Сколько нужно собрать? Укажите сумму числом, например 15000."),
            ("target_amount", "В какой валюте сбор: RUB или BYN?"),
            ("currency", "Ссылка на документы/смету/счёт (если есть). Если нет, напишите «Нет»."),
            ("evidence", "Ссылка на фото животного (если есть). Если нет, напишите «Нет»."),
            ("photo_url", "Контакт ответственного человека (Telegram или телефон)."),
            ("contact", None)
        ]
        key, next_prompt = fields[step]
        if key == "country" and incoming not in ("Россия", "Беларусь"):
            await telegram_send(str(chat_id), "Пожалуйста, ответьте точно: Россия или Беларусь.")
            return {"ok": True}
        if key == "need_type" and incoming not in ("Лечение", "Корм", "Лечение и корм", "Другое"):
            await telegram_send(str(chat_id), "Выберите один вариант: Лечение, Корм, Лечение и корм или Другое.")
            return {"ok": True}
        if key == "target_amount":
            try:
                amount = float(incoming.replace(",", ".").replace(" ", ""))
                if amount <= 0:
                    raise ValueError()
                payload[key] = amount
            except ValueError:
                await telegram_send(str(chat_id), "Введите положительную сумму числом, например 15000.")
                return {"ok": True}
        elif key == "currency" and incoming not in ("RUB", "BYN"):
            await telegram_send(str(chat_id), "Ответьте RUB или BYN.")
            return {"ok": True}
        else:
            payload[key] = "" if incoming.lower() in ("нет", "не знаю") else incoming
        next_step = step + 1
        if next_step >= len(fields):
            required = ["animal_name","city","country","story","need_type","target_amount","currency","contact"]
            if any(not str(payload.get(k, "")).strip() for k in required):
                await telegram_send(str(chat_id), "Не хватает обязательных данных. Начните заново командой /start.")
                con.execute("DELETE FROM bot_drafts WHERE chat_id=?", (chat_id,))
                con.commit()
                return {"ok": True}
            cur = con.execute("""
                INSERT INTO cases
                (animal_name,city,country,story,need_type,clinic,target_amount,currency,evidence,photo_url,contact)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, tuple(payload.get(k, "") for k in [
                "animal_name","city","country","story","need_type","clinic","target_amount",
                "currency","evidence","photo_url","contact"
            ]))
            con.execute("DELETE FROM bot_drafts WHERE chat_id=?", (chat_id,))
            con.commit()
            case_id = cur.lastrowid
            await notify_admin(
                f"Новая заявка из Telegram №{case_id}\nЖивотное: {payload['animal_name']}\n"
                f"Нужда: {payload['need_type']}\nГород: {payload['city']}, {payload['country']}\n"
                f"Сумма: {payload['target_amount']} {payload['currency']}\n"
                f"Клиника: {payload.get('clinic','')}\nДокументы: {payload.get('evidence','')}\n"
                f"Фото: {payload.get('photo_url','')}\nКонтакт: {payload['contact']}\n"
                f"История: {payload['story'][:700]}"
            )
            await telegram_send(str(chat_id),
                f"Спасибо! Заявка №{case_id} принята на проверку. 🐾\n"
                "Сбор появится на сайте только после проверки истории и подтверждения стоимости. "
                "Пока не отправляйте деньги по непроверенным реквизитам.")
            return {"ok": True}
        con.execute("UPDATE bot_drafts SET step=?,payload=?,updated_at=CURRENT_TIMESTAMP WHERE chat_id=?",
                    (next_step, json.dumps(payload, ensure_ascii=False), chat_id))
        con.commit()
    await telegram_send(str(chat_id), next_prompt or "Спасибо! Заявка получена.")
    return {"ok": True}
