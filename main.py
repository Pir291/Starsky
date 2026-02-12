import os
import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import json
import secrets
import string

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramNetworkError

from db import (
    create_or_update_user,
    update_last_activity,
    get_all_users_from_db,
    update_info_in_db,
    get_user_by_telegram_id,
    save_public_message,
    get_public_messages,
    upsert_star_state,
    get_all_star_states,
    set_login_code,
    get_connection,
    get_star_state,
)

import uvicorn

# ================== CONFIG ==================

BOT_TOKEN = "8127084344:AAHPVcpT2-USGSUQftgSR0OzCXlhO1fi5TA"
  # обязательно задать в Render

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ================== WS: stars ==================

class StarsWSManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_json(self, data: dict):
        to_remove = []
        for ws in self.active_connections:
            try:
                await ws.send_json(data)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.disconnect(ws)


ws_manager = StarsWSManager()

# in-memory кэш
users: Dict[int, Dict] = {}


def get_last_active(user: Dict) -> Optional[datetime]:
    iso = user.get("last_active_iso")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def set_last_active(user: Dict):
    user["last_active_iso"] = datetime.utcnow().isoformat()


def is_active(user: Dict) -> bool:
    last = get_last_active(user)
    if not last:
        return False
    return datetime.utcnow() - last <= timedelta(minutes=1)


def inc_activity(user: Dict, amount: float = 1.0):
    score = float(user.get("activity_score", 0))
    score = max(score + amount, 0.0)
    user["activity_score"] = score


def dec_activity_all(amount: float = 1.0):
    for u in users.values():
        score = float(u.get("activity_score", 0))
        score = max(score - amount, 0.0)
        u["activity_score"] = score


def generate_login_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def sync_star_state_to_db(user_id: int):
    u = users.get(user_id)
    if not u:
        return
    activity_score = float(u.get("activity_score", 0.0))
    star_color = u.get("star_color") or "#ffffff"
    star_shape = u.get("star_shape") or "circle"
    info = u.get("info") or ""
    skins_owned = u.get("skins_owned", []) or []
    try:
        upsert_star_state(
            user_id=user_id,
            activity_score=activity_score,
            star_color=star_color,
            star_shape=star_shape,
            info=info,
            skins_owned=skins_owned,
        )
    except Exception as e:
        print("DEBUG sync_star_state_to_db error:", user_id, e)


def ensure_user_cached(user_id: int):
    if user_id in users:
        return

    db_user = get_user_by_telegram_id(user_id)
    star = get_star_state(user_id)

    username = (db_user["username"] if db_user else None) or f"user_{user_id}"
    full_name = username

    activity_score = float(star["activity_score"]) if star else 0.0
    star_color = star["star_color"] if star else "#ffffff"
    star_shape = star["star_shape"] if star else "circle"
    info = ""
    if star and star.get("info"):
        info = star["info"]
    elif db_user and db_user.get("info"):
        info = db_user["info"]

    skins = []
    if star and star.get("skins_owned"):
        skins = star["skins_owned"]

    users[user_id] = {
        "id": user_id,
        "username": username,
        "full_name": full_name,
        "activity_score": activity_score,
        "star_color": star_color,
        "star_shape": star_shape,
        "skins_owned": skins,
        "info": info,
    }


# ================== Публичный чат API ==================

@app.get("/api/public_chat")
async def api_public_chat():
    msgs = get_public_messages(limit=200)
    return {
        "messages": [
            {"username": m["username"], "text": m["text"]}
            for m in msgs
        ]
    }


# ================== Анонимный чат в боте ==================

waiting_user_id: Optional[int] = None
pairs: Dict[int, int] = {}


def get_partner(user_id: int) -> Optional[int]:
    return pairs.get(user_id)


def break_pair(user_id: int) -> None:
    partner = pairs.pop(user_id, None)
    if partner is not None:
        pairs.pop(partner, None)


def set_pair(user1: int, user2: int) -> None:
    pairs[user1] = user2
    pairs[user2] = user1


def build_chat_menu_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти собеседника", callback_data="chat_find")
    kb.button(text="⛔ Остановить чат", callback_data="chat_stop")
    kb.adjust(1)
    return kb


@router.message(Command("chat"))
async def cmd_chat_menu(message: Message):
    kb = build_chat_menu_keyboard()
    await message.answer(
        "Анонимный чат (рандомный собеседник):\n"
        "• «Найти собеседника» — поиск пары.\n"
        "• «Остановить чат» — разорвать диалог.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "chat_find")
async def cb_chat_find(callback: CallbackQuery):
    global waiting_user_id

    user_id = callback.from_user.id

    if user_id in pairs:
        await callback.answer("Ты уже общаешься с собеседником.", show_alert=True)
        return

    if waiting_user_id == user_id:
        await callback.answer("Ты уже в очереди, ждём собеседника…", show_alert=False)
        return

    if waiting_user_id is not None and waiting_user_id != user_id:
        partner_id = waiting_user_id
        waiting_user_id = None

        set_pair(user_id, partner_id)

        try:
            await bot.send_message(
                partner_id,
                "🎭 Собеседник найден! Можешь писать, сообщения будут пересылаться анонимно.",
            )
        except Exception:
            break_pair(user_id)
            await callback.answer(
                "Не удалось соединить с собеседником, попробуй ещё раз.",
                show_alert=True,
            )
            return

        await callback.message.answer(
            "🎭 Собеседник найден! Можешь писать, сообщения будут пересылаться анонимно."
        )
        await callback.answer()
        return

    waiting_user_id = user_id
    await callback.message.answer("⌛ Ты добавлен в очередь. Ждём второго собеседника…")
    await callback.answer()


@router.callback_query(F.data == "chat_stop")
async def cb_chat_stop(callback: CallbackQuery):
    global waiting_user_id
    user_id = callback.from_user.id

    if waiting_user_id == user_id:
        waiting_user_id = None
        await callback.message.answer("⛔ Поиск остановлен, ты больше не в очереди.")
        await callback.answer()
        return

    partner = get_partner(user_id)
    if partner:
        break_pair(user_id)
        try:
            await bot.send_message(partner, "❌ Собеседник завершил диалог.")
        except Exception:
            pass
        await callback.message.answer("⛔ Ты завершил анонимный диалог.")
        await callback.answer()
        return

    await callback.answer("Ты сейчас ни с кем не общаешься.", show_alert=True)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ================== Чат на сайте (WS) ==================

class SiteChatManager:
    def __init__(self):
        self.broadcast_clients: List[WebSocket] = []
        self.user_sockets: Dict[int, WebSocket] = {}
        self.private_pairs: Dict[int, int] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.broadcast_clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.broadcast_clients:
            self.broadcast_clients.remove(ws)

        to_remove = []
        for uid, sock in self.user_sockets.items():
            if sock is ws:
                to_remove.append(uid)
        for uid in to_remove:
            self.user_sockets.pop(uid, None)
            partner = self.private_pairs.pop(uid, None)
            if partner is not None:
                self.private_pairs.pop(partner, None)

    def bind_user_socket(self, user_id: int, ws: WebSocket):
        self.user_sockets[user_id] = ws

    async def handle_public_message(self, ws: WebSocket, text: str, user_id: int):
        ensure_user_cached(user_id)
        u = users.get(user_id)
        if not u:
            return

        username = u["username"]

        save_public_message(user_id, username, text)

        for client in list(self.broadcast_clients):
            try:
                await client.send_json({
                    "type": "public",
                    "username": username,
                    "text": text
                })
            except Exception:
                try:
                    self.broadcast_clients.remove(client)
                except ValueError:
                    pass

        set_last_active(u)
        inc_activity(u, 2.0)
        sync_star_state_to_db(user_id)
        await ws_manager.broadcast_json({
            "type": "activity_update",
            "username": username,
            "active": True,
            "activity_score": float(u.get("activity_score", 0.0)),
            "star_color": u.get("star_color") or "#ffffff",
            "star_shape": u.get("star_shape") or "circle",
        })

    async def handle_private_request(
        self,
        ws: WebSocket,
        from_id: int,
        to_id: int,
    ):
        target_ws = self.user_sockets.get(to_id)
        if not target_ws:
            await ws.send_json({
                "type": "system",
                "message": "Пользователь не в чате.",
            })
            return

        await target_ws.send_json({
            "type": "private_request",
            "from_id": from_id,
            "from_username": users.get(from_id, {}).get("username", "пользователь"),
            "to_id": to_id,
        })

    async def handle_private_response(
        self,
        ws: WebSocket,
        accepted: bool,
        from_id: int,
        to_id_raw,
    ):
        try:
            to_id = int(to_id_raw)
        except Exception:
            return

        target_ws = self.user_sockets.get(to_id)
        if not target_ws:
            return

        await target_ws.send_json({
            "type": "private_response",
            "accepted": accepted,
            "from_id": from_id,
            "from_username": users.get(from_id, {}).get("username", "пользователь"),
            "to_id": to_id,
        })

        if accepted:
            self.private_pairs[from_id] = to_id
            self.private_pairs[to_id] = from_id

    async def handle_private_message(
        self,
        ws: WebSocket,
        text: str,
        user_id: int,
        partner_id: Optional[int],
    ):
        if partner_id is None:
            await ws.send_json({
                "type": "system",
                "message": "Нет выбранного собеседника для приватного чата.",
            })
            return

        ensure_user_cached(user_id)
        ensure_user_cached(partner_id)

        if user_id not in users or partner_id not in users:
            await ws.send_json({
                "type": "system",
                "message": "Собеседник не найден.",
            })
            return

        if self.private_pairs.get(user_id) != partner_id:
            await ws.send_json({
                "type": "system",
                "message": "Приватный чат ещё не подтверждён.",
            })
            return

        partner_ws = self.user_sockets.get(partner_id)
        if not partner_ws:
            await ws.send_json({
                "type": "system",
                "message": "Собеседник офлайн.",
            })
            return

        u = users[user_id]
        username = u["username"]

        await partner_ws.send_json({
            "type": "private",
            "from_id": user_id,
            "to_id": partner_id,
            "username": username,
            "text": text,
        })

        set_last_active(u)
        inc_activity(u, 3.0)
        sync_star_state_to_db(user_id)
        await ws_manager.broadcast_json({
            "type": "activity_update",
            "username": username,
            "active": True,
            "activity_score": float(u.get("activity_score", 0.0)),
            "star_color": u.get("star_color") or "#ffffff",
            "star_shape": u.get("star_shape") or "circle",
        })


site_chat_manager = SiteChatManager()


@app.websocket("/ws_chat")
async def ws_chat(websocket: WebSocket):
    await site_chat_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()

            msg_type = data.get("type")
            text = (data.get("text") or "").strip()
            user_id_raw = data.get("user_id")

            try:
                user_id = int(user_id_raw) if user_id_raw is not None else None
            except Exception:
                user_id = None

            if user_id is not None:
                site_chat_manager.bind_user_socket(user_id, websocket)
                ensure_user_cached(user_id)

            if msg_type == "private_request":
                if user_id is not None:
                    to_id_raw = data.get("to_id")
                    try:
                        to_id = int(to_id_raw)
                    except Exception:
                        to_id = None
                    if to_id is not None:
                        await site_chat_manager.handle_private_request(
                            websocket,
                            from_id=user_id,
                            to_id=to_id,
                        )
                continue

            if msg_type == "private_response":
                if user_id is not None:
                    await site_chat_manager.handle_private_response(
                        websocket,
                        accepted=bool(data.get("accepted")),
                        from_id=user_id,
                        to_id_raw=data.get("to_id"),
                    )
                continue

            if msg_type != "message":
                continue
            if not text:
                continue

            if user_id is None:
                await websocket.send_json({
                    "type": "system",
                    "message": "Чтобы писать в чат, войди через код из бота."
                })
                continue

            ensure_user_cached(user_id)
            if user_id not in users:
                await websocket.send_json({
                    "type": "system",
                    "message": "Чтобы писать в чат, зайди через бота и появись на небе."
                })
                continue

            mode = data.get("mode")
            if mode == "public":
                await site_chat_manager.handle_public_message(websocket, text, user_id)
            else:
                partner_id_raw = data.get("partner_id")
                try:
                    partner_id = int(partner_id_raw) if partner_id_raw is not None else None
                except Exception:
                    partner_id = None
                await site_chat_manager.handle_private_message(
                    websocket, text, user_id, partner_id
                )

    except WebSocketDisconnect:
        site_chat_manager.disconnect(websocket)


# ================== API: звёзды, логин, скины, info ==================

@app.get("/api/stars")
async def get_stars():
    star_rows = get_all_star_states()
    db_users = get_all_users_from_db()
    db_by_id = {row["telegram_id"]: row for row in db_users}

    result = []

    for row in star_rows:
        tg_id = row["user_id"]
        db_row = db_by_id.get(tg_id)
        local = users.get(tg_id, {})

        username = (
            local.get("username")
            or (db_row["username"] if db_row else None)
            or f"user_{tg_id}"
        )
        full_name = local.get("full_name", username)

        if row.get("info"):
            info = row["info"]
        elif db_row and db_row.get("info"):
            info = db_row["info"]
        else:
            info = local.get("info") or f"{full_name} уже на небе"

        active_flag = tg_id in users and is_active(users[tg_id])

        result.append(
            {
                "id": tg_id,
                "username": username,
                "info": info,
                "active": active_flag,
                "activity_score": float(row.get("activity_score", 0.0)),
                "star_color": row.get("star_color") or "#ffffff",
                "star_shape": row.get("star_shape") or "circle",
            }
        )

    return JSONResponse(result)


@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    code = (data.get("code") or "").strip().upper()
    if not code:
        return JSONResponse({"ok": False, "error": "empty_code"}, status_code=400)

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT u.telegram_id AS id,
                   u.username,
                   u.info,
                   s.activity_score,
                   s.star_color,
                   s.star_shape,
                   s.skins_owned
            FROM users u
            LEFT JOIN user_stars s ON s.user_id = u.telegram_id
            WHERE u.login_code = %s
            """,
            (code,)
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        return JSONResponse({"ok": False, "error": "invalid_code"}, status_code=404)

    set_login_code(row["id"], None)

    skins = []
    skins_raw = row.get("skins_owned")
    if isinstance(skins_raw, str):
        try:
            skins = json.loads(skins_raw)
        except Exception:
            skins = []
    elif isinstance(skins_raw, list):
        skins = skins_raw

    return {
        "ok": True,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "full_name": row["username"],
            "activity_score": float(row.get("activity_score") or 0.0),
            "star_color": row.get("star_color") or "#ffffff",
            "star_shape": row.get("star_shape") or "circle",
            "skins_owned": skins,
            "info": row.get("info") or "",
        },
    }


STAR_SKINS = {
    "gold_color":    {"color": "#facc15", "cost": 30, "shape": None,       "type": "color"},
    "blue_color":    {"color": "#38bdf8", "cost": 30, "shape": None,       "type": "color"},
    "pink_color":    {"color": "#ec4899", "cost": 30, "shape": None,       "type": "color"},
    "green_color":   {"color": "#22c55e", "cost": 30, "shape": None,       "type": "color"},

    "diamond_shape": {"color": None,      "cost": 40, "shape": "diamond",  "type": "shape"},
    "cross_shape":   {"color": None,      "cost": 40, "shape": "cross",    "type": "shape"},
    "triangle_shape":{"color": None,      "cost": 50, "shape": "triangle", "type": "shape"},
    "ring_shape":    {"color": None,      "cost": 60, "shape": "ring",     "type": "shape"},
    "pulsar_combo":  {"color": "#f97316", "cost": 80, "shape": "pulsar",   "type": "both"},
}


@app.get("/api/skins")
async def api_skins():
    return {
        "skins": [
            {
                "id": key,
                "color": data["color"],
                "cost": data["cost"],
                "shape": data["shape"],
                "type": data["type"],
            }
            for key, data in STAR_SKINS.items()
        ]
    }


@app.post("/api/buy_skin")
async def api_buy_skin(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    skin_id = data.get("skin_id")

    if user_id is None or skin_id not in STAR_SKINS:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    try:
        user_id = int(user_id)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_user_id"}, status_code=400)

    ensure_user_cached(user_id)
    user = users.get(user_id)
    if not user:
        return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=404)

    user.setdefault("skins_owned", [])

    skin = STAR_SKINS[skin_id]
    cost = skin["cost"]
    skin_type = skin["type"]

    if skin_id in user["skins_owned"]:
        if skin_type in ("both", "color") and skin["color"]:
            user["star_color"] = skin["color"]
        if skin_type in ("both", "shape") and skin["shape"]:
            user["star_shape"] = skin["shape"]
        sync_star_state_to_db(user_id)
    else:
        if float(user.get("activity_score", 0.0)) < cost:
            return JSONResponse({"ok": False, "error": "not_enough_activity"}, status_code=400)

        user["activity_score"] = float(user.get("activity_score", 0.0)) - cost
        user["skins_owned"].append(skin_id)

        if skin_type in ("both", "color") and skin["color"]:
            user["star_color"] = skin["color"]
        if skin_type in ("both", "shape") and skin["shape"]:
            user["star_shape"] = skin["shape"]

        sync_star_state_to_db(user_id)

    await ws_manager.broadcast_json({
        "type": "activity_update",
        "username": user["username"],
        "active": True,
        "activity_score": float(user["activity_score"]),
        "star_color": user.get("star_color") or "#ffffff",
        "star_shape": user.get("star_shape") or "circle",
    })

    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "activity_score": float(user["activity_score"]),
            "star_color": user.get("star_color") or "#ffffff",
            "star_shape": user.get("star_shape") or "circle",
            "skins_owned": user.get("skins_owned", []),
        }
    }


@app.post("/api/update_info")
async def api_update_info(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    info = (data.get("info") or "").strip()

    if not user_id:
        return JSONResponse({"ok": False, "error": "no_user_id"}, status_code=400)

    try:
        user_id = int(user_id)
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_user_id"}, status_code=400)

    ensure_user_cached(user_id)
    if user_id not in users:
        return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=404)

    if len(info) > 100:
        return JSONResponse({"ok": False, "error": "too_long"}, status_code=400)

    users[user_id]["info"] = info
    sync_star_state_to_db(user_id)
    update_info_in_db(user_id, info)

    return {"ok": True}


# ================== Бот ==================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user

    username = user.username or f"user_{user.id}"
    full_name = user.full_name
    is_new = user.id not in users

    ensure_user_cached(user.id)
    u = users[user.id]
    u["username"] = username
    u["full_name"] = full_name
    u.setdefault("star_color", "#ffffff")
    u.setdefault("star_shape", "circle")
    u.setdefault("skins_owned", [])
    u.setdefault("info", "")

    set_last_active(u)
    inc_activity(u, 3.0)
    sync_star_state_to_db(user.id)

    create_or_update_user(
        telegram_id=user.id,
        username=username,
        info=u.get("info") or ""
    )

    await message.answer(
        "Привет! Теперь ты звезда на нашем небе ⭐️\n"
        f"Твой ник: @{username}\n\n"
        "Напиши /login, чтобы получить код входа на сайт Star Users."
    )

    if is_new:
        data = {
            "type": "new_star",
            "id": user.id,
            "username": username,
            "info": f"{full_name} только что появился на небе",
            "active": True,
            "activity_score": float(u.get("activity_score", 0.0)),
            "star_color": u.get("star_color") or "#ffffff",
            "star_shape": u.get("star_shape") or "circle",
        }
        await ws_manager.broadcast_json(data)


@router.message(Command("login"))
async def cmd_login(message: Message):
    user = message.from_user
    ensure_user_cached(user.id)
    info = users.get(user.id)
    if not info:
        await message.answer("Сначала напиши /start, чтобы появиться на небе.")
        return

    code = generate_login_code()
    set_login_code(user.id, code)

    await message.answer(
        "Код для входа на сайт Star Users:\n"
        f"`{code}`\n\n"
        "Открой сайт и введи этот код в поле «Код входа через бота».",
        parse_mode="Markdown"
    )


@router.message(F.text == "/me")
async def cmd_me(message: Message):
    user = message.from_user
    ensure_user_cached(user.id)
    info = users.get(user.id)
    if not info:
        await message.answer("Ты ещё не зарегистрирован. Напиши /start")
        return

    await message.answer(
        f"Ты уже на небе как @{info['username']} ✨\n"
        f"Твой уровень активности: {int(info.get('activity_score', 0))}\n"
        f"Цвет звезды: {info.get('star_color', '#ffffff')}\n"
        f"Форма звезды: {info.get('star_shape', 'circle')}\n"
        f"Купленные скины: {', '.join(info.get('skins_owned', [])) or 'нет'}\n"
        f"Описание: {info.get('info') or 'не задано'}"
    )


@router.message()
async def any_message(message: Message):
    user = message.from_user
    user_id = user.id
    text = message.text or ""

    partner_id = get_partner(user_id)
    if partner_id:
        try:
            await bot.send_message(partner_id, f"💬 Собеседник: {text}")
        except Exception:
            break_pair(user_id)
            await message.answer(
                "Не удалось доставить сообщение собеседнику, диалог остановлен."
            )

    username = user.username or f"user_{user.id}"
    full_name = user.full_name

    ensure_user_cached(user.id)
    u = users[user.id]
    u["username"] = username
    u["full_name"] = full_name
    u.setdefault("star_color", "#ffffff")
    u.setdefault("star_shape", "circle")
    u.setdefault("skins_owned", [])
    u.setdefault("info", "")

    set_last_active(u)
    inc_activity(u, 1.0)
    sync_star_state_to_db(user.id)
    update_last_activity(user.id)

    data = {
        "type": "activity_update",
        "id": user.id,
        "username": username,
        "active": True,
        "activity_score": float(u.get("activity_score", 0.0)),
        "star_color": u.get("star_color") or "#ffffff",
        "star_shape": u.get("star_shape") or "circle",
    }
    await ws_manager.broadcast_json(data)


# ================== Служебные циклы и main ==================

async def activity_decay_loop():
    while True:
        await asyncio.sleep(10)
        if users:
            dec_activity_all(0.5)
            # при желании можно здесь же вызывать sync_star_state_to_db по всем


async def run_bot():
    while True:
        try:
            await dp.start_polling(bot)
        except TelegramNetworkError as e:
            print("TelegramNetworkError, retry in 10s:", e)
            await asyncio.sleep(10)
        except Exception as e:
            print("Unexpected error in bot:", e)
            break


async def main():
    bot_task = asyncio.create_task(run_bot())

    port = int(os.environ.get("PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, reload=False)
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    decay_task = asyncio.create_task(activity_decay_loop())

    await asyncio.gather(bot_task, api_task, decay_task)


if __name__ == "__main__":
    asyncio.run(main())
