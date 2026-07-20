"""
Онлайн примерка — отдельное приложение.

Поток из 4 шагов:
  1) Загрузка фото одежды (верх + низ обязательно, максимум 6 предметов верх/низ;
     дополнительно — головной убор, обувь, аксессуары). Без коррекции цвета/вида.
  2) Оплата через ЮKassa.
  3) Онлайн-примерка: клиент выбирает один из трёх ИИ-манекенов, одежда «надевается»
     на движущийся манекен через видео-примерку WaveSpeedAI (1 образ = 1 видео).
  4) Выдача образа (видео).

Ключи и секреты берутся ТОЛЬКО из переменных окружения (в код и в браузер не попадают).
Рекомендации/видео выдаются сервером лишь после подтверждённой оплаты.
"""

import os
import json
import uuid
import base64
import time
import threading
import tempfile
from contextlib import closing

import sqlite3
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from typing import Optional, List

# ЮKassa
try:
    from yookassa import Configuration, Payment
    _YK = True
except Exception:
    _YK = False

# ------------------------------------------------------------------ Конфиг ----
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
SITE_URL = os.getenv("SITE_URL", "https://example.twc1.net").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "RUB")
DEFAULT_PRICE = os.getenv("SERVICE_PRICE", "99.00")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_TOKEN = uuid.uuid4().hex  # обновляется при каждом запуске сервера

# WaveSpeedAI — примерка. Две модели: быстрая картинка и видео.
WAVESPEED_API_KEY = os.getenv("WAVESPEED_API_KEY", "").strip()
WAVESPEED_MODEL = os.getenv("WAVESPEED_MODEL", "wavespeed-ai/ai-virtual-outfit-tryon").strip()
# «Видео» — движущийся манекен (~1–2 мин). «Фото» — быстрая картинка (~15–30 сек).
WAVESPEED_VIDEO_MODEL = os.getenv("WAVESPEED_VIDEO_MODEL", WAVESPEED_MODEL).strip()
WAVESPEED_PHOTO_MODEL = os.getenv("WAVESPEED_PHOTO_MODEL", "wavespeed-ai/ai-clothes-changer").strip()
WAVESPEED_BASE = os.getenv("WAVESPEED_BASE", "https://api.wavespeed.ai/api/v3").rstrip("/")
TRYON_DURATION = int(os.getenv("TRYON_DURATION", "5"))
TRYON_PROMPT = os.getenv("TRYON_PROMPT", "").strip()

if _YK and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("ORDERS_DB") or os.path.join(tempfile.gettempdir(), "primerka_orders.db")
IMG_DIR = os.path.join(tempfile.gettempdir(), "primerka_imgs")
os.makedirs(IMG_DIR, exist_ok=True)

# Простое хранилище задач примерки в памяти (на один процесс этого достаточно).
TASKS = {}

# --------------------------------------------------------------------- БД -----
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    try:
        with closing(db()) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS orders(
                    order_id   TEXT PRIMARY KEY,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    paid       INTEGER NOT NULL DEFAULT 0,
                    amount     TEXT,
                    is_test    INTEGER NOT NULL DEFAULT 0,
                    payment_id TEXT,
                    created_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY, value TEXT
                )"""
            )
            conn.commit()
    except Exception as e:
        print("init_db warning:", e)


init_db()


def get_setting(key, default=None):
    try:
        with closing(db()) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
    except Exception:
        return default


def set_setting(key, value):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def effective_price():
    return get_setting("price") or DEFAULT_PRICE


def test_mode():
    return (get_setting("test_mode") or "0") == "1"


def payments_ready():
    return bool(_YK and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def effective_payments_ready():
    return payments_ready() and not test_mode()


def get_order(order_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def set_paid(order_id):
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET paid=1 WHERE order_id=?", (order_id,))
        conn.commit()


# ---------------------------------------------------------------- Манекены ----
def mannequin_frame(n):
    """Стоп-кадр манекена (портрет для примерки): model-1.jpg .. model-3.jpg."""
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = os.path.join(HERE, f"model-{n}.{ext}")
        if os.path.exists(p):
            return p
    return None


def mannequin_video(n):
    """Необязательное видео манекена для красивой заставки: mannequin-1.mp4 .. -3.mp4."""
    for ext in ("mp4", "webm", "mov"):
        p = os.path.join(HERE, f"mannequin-{n}.{ext}")
        if os.path.exists(p):
            return p
    return None


def available_mannequins():
    out = []
    for n in (1, 2, 3):
        if mannequin_frame(n):
            out.append({"id": n, "hasVideo": mannequin_video(n) is not None})
    return out


def tryon_enabled():
    return bool(WAVESPEED_API_KEY) and len(available_mannequins()) > 0


# ------------------------------------------------------------- Приложение -----
app = FastAPI(title="Online Primerka")
app.add_middleware(
    CORSMiddleware, allow_origins=[SITE_URL, "*"],
    allow_methods=["*"], allow_headers=["*"],
)


def _page(name):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        return FileResponse(p)
    return JSONResponse({"detail": f"{name} не найден рядом с main.py"}, status_code=404)


@app.get("/")
def index():
    return _page("index.html")


@app.get("/oferta")
def p_oferta():
    return _page("oferta.html")


@app.get("/politika")
def p_politika():
    return _page("politika.html")


@app.get("/kontakty")
def p_kontakty():
    return _page("kontakty.html")


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/model/{n}")
def model_frame(n: int):
    p = mannequin_frame(n)
    if p:
        return FileResponse(p)
    raise HTTPException(404, "манекен не найден")


@app.get("/mannequin/{n}")
def model_video(n: int):
    p = mannequin_video(n)
    if p:
        return FileResponse(p)
    raise HTTPException(404, "видео манекена не найдено")


@app.get("/api/img/{token}")
def serve_img(token: str):
    """Публичная ссылка на загруженное фото одежды — нужна, чтобы WaveSpeedAI
    смог скачать изображение по URL. Токен случайный, файлы временные."""
    safe = "".join(c for c in token if c.isalnum() or c in "-_")
    p = os.path.join(IMG_DIR, safe + ".jpg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(404, "not found")


@app.get("/api/config")
def config():
    return {
        "price": effective_price(),
        "currency": CURRENCY,
        "paymentsEnabled": effective_payments_ready(),
        "tryonEnabled": tryon_enabled(),
        "mannequins": available_mannequins(),
        "minItems": 2,
        "maxItems": 6,
    }


# ------------------------------------------------------------------ Оплата ----
class CreatePaymentIn(BaseModel):
    itemCount: int = 0
    garments: List[str] = []      # dataURL фото одежды — сохраняем на сервере


@app.post("/api/create-payment")
def create_payment(data: CreatePaymentIn):
    if not effective_payments_ready():
        raise HTTPException(503, "Платежи не настроены или включён тестовый режим")
    price = effective_price()
    order_id = uuid.uuid4().hex
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO orders(order_id, item_count, paid, amount, is_test, created_at) "
            "VALUES(?,?,0,?,0,?)",
            (order_id, int(data.itemCount or 0), price, now_iso()),
        )
        conn.commit()

    # Сохраняем фото одежды на сервере — чтобы примерка работала после возврата
    # с оплаты (СБП) независимо от браузера/вкладки, где загружали фото.
    _store_garments(order_id, data.garments)

    payment = Payment.create(
        {
            "amount": {"value": price, "currency": CURRENCY},
            "capture": True,
            "confirmation": {"type": "redirect",
                             "return_url": f"{SITE_URL}/?order_id={order_id}"},
            "description": "Онлайн примерка одежды (видео-образ)",
            "metadata": {"order_id": order_id},
            # Продавец — самозанятый (НПД): фискальный чек по 54-ФЗ не формируется здесь.
        },
        uuid.uuid4().hex,
    )
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET payment_id=? WHERE order_id=?", (payment.id, order_id))
        conn.commit()
    return {"order_id": order_id, "confirmation_url": payment.confirmation.confirmation_url}


def verify_and_mark(order):
    if order["paid"]:
        return True
    if not (payments_ready() and order.get("payment_id")):
        return False
    try:
        p = Payment.find_one(order["payment_id"])
        if getattr(p, "status", None) == "succeeded":
            set_paid(order["order_id"])
            return True
    except Exception:
        return False
    return False


@app.get("/api/order/{order_id}")
def order_status(order_id: str):
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return {"paid": bool(verify_and_mark(order))}


# --------------------------------------------------------------- Примерка -----
class TryOnIn(BaseModel):
    order_id: str
    mannequin: int = 1
    kind: str = "video"               # "photo" (быстро) или "video"
    garments: List[str] = []          # dataURL картинок одежды (верх, низ, доп.)


def _save_dataurl(data_url):
    """Сохраняет dataURL в файл и возвращает публичный URL для WaveSpeedAI."""
    if "," in data_url:
        head, b64 = data_url.split(",", 1)
    else:
        b64 = data_url
    raw = base64.b64decode(b64)
    token = uuid.uuid4().hex
    with open(os.path.join(IMG_DIR, token + ".jpg"), "wb") as f:
        f.write(raw)
    return f"{SITE_URL}/api/img/{token}"


def _garment_index_path(order_id):
    return os.path.join(IMG_DIR, f"order_{order_id}.json")


def _store_garments(order_id, garments):
    """Сохраняет фото одежды заказа на сервере и запоминает их публичные ссылки."""
    urls = []
    for g in (garments or []):
        if not g:
            continue
        try:
            urls.append(_save_dataurl(g))
        except Exception:
            pass
        if len(urls) >= 8:
            break
    if urls:
        try:
            with open(_garment_index_path(order_id), "w") as f:
                json.dump(urls, f)
        except Exception as e:
            print("store garments warn:", e)
    return urls


def stored_garment_urls(order_id):
    try:
        with open(_garment_index_path(order_id)) as f:
            return json.load(f)
    except Exception:
        return []


def _wavespeed_create(portrait_url, clothes_urls, model, with_duration):
    url = f"{WAVESPEED_BASE}/{model}"
    body = {"image": portrait_url, "clothes_images": clothes_urls}
    if with_duration:
        body["duration"] = TRYON_DURATION
    if TRYON_PROMPT:
        body["prompt"] = TRYON_PROMPT
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {WAVESPEED_API_KEY}",
                 "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    data = j.get("data", j)
    pred_id = data.get("id") or j.get("id")
    poll_url = (data.get("urls", {}) or {}).get("get") if isinstance(data.get("urls"), dict) else None
    if not poll_url and pred_id:
        poll_url = f"{WAVESPEED_BASE}/predictions/{pred_id}/result"
    return pred_id, poll_url


def _wavespeed_poll(poll_url):
    r = requests.get(
        poll_url,
        headers={"Authorization": f"Bearer {WAVESPEED_API_KEY}"},
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    data = j.get("data", j)
    status = (data.get("status") or j.get("status") or "").lower()
    out = data.get("outputs") or data.get("output") or j.get("output")
    video = None
    if isinstance(out, list) and out:
        video = out[0]
    elif isinstance(out, str):
        video = out
    return status, video


def _run_tryon(task_id, mannequin, clothes_urls, kind):
    TASKS[task_id] = {"status": "processing", "url": None, "kind": kind, "error": None}
    try:
        portrait = f"{SITE_URL}/model/{mannequin}"
        clothes = [u for u in (clothes_urls or []) if u][:8]
        if not clothes:
            raise RuntimeError("нет изображений одежды")
        is_video = (kind == "video")
        model = WAVESPEED_VIDEO_MODEL if is_video else WAVESPEED_PHOTO_MODEL
        pred_id, poll_url = _wavespeed_create(portrait, clothes, model, is_video)
        if not poll_url:
            raise RuntimeError("WaveSpeed: не получен идентификатор задачи")
        # Видео обычно ~60–120 сек, картинка — ~10–30 сек.
        max_polls = 80 if is_video else 30
        for _ in range(max_polls):
            time.sleep(3)
            status, url = _wavespeed_poll(poll_url)
            if status in ("completed", "succeeded", "success") and url:
                TASKS[task_id] = {"status": "done", "url": url, "kind": kind, "error": None}
                return
            if status in ("failed", "error", "canceled"):
                raise RuntimeError(f"WaveSpeed вернул статус: {status}")
        raise RuntimeError("превышено время ожидания результата")
    except Exception as e:
        TASKS[task_id] = {"status": "error", "url": None, "kind": kind, "error": str(e)}


@app.post("/api/tryon")
def start_tryon(data: TryOnIn):
    order = get_order(data.order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    if not (order["is_test"] or verify_and_mark(order)):
        raise HTTPException(402, "Оплата не найдена или ещё не подтверждена")
    if not tryon_enabled():
        raise HTTPException(503, "Примерка не настроена (нет ключа WaveSpeed или манекенов)")
    # Картинки одежды: сначала берём сохранённые на сервере при оплате;
    # если их нет (демо/тест) — принимаем из запроса и сохраняем.
    clothes = stored_garment_urls(data.order_id)
    if not clothes and data.garments:
        clothes = _store_garments(data.order_id, data.garments)
    if not clothes:
        raise HTTPException(400, "Нет фото одежды")
    mannequin = data.mannequin if mannequin_frame(data.mannequin) else (
        available_mannequins()[0]["id"] if available_mannequins() else 1)
    kind = "photo" if str(data.kind).lower() == "photo" else "video"
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"status": "processing", "url": None, "kind": kind, "error": None}
    threading.Thread(target=_run_tryon, args=(task_id, mannequin, clothes, kind),
                     daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/tryon/{task_id}")
def tryon_status(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    return t


# ------------------------------------------------------------- Тест-заказ -----
class TestOrderIn(BaseModel):
    itemCount: int = 0


@app.post("/api/test-order")
def test_order(data: TestOrderIn):
    """Создаёт тестовый оплаченный заказ (только в тестовом/демо-режиме)."""
    if effective_payments_ready():
        raise HTTPException(403, "Недоступно в боевом режиме")
    order_id = uuid.uuid4().hex
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO orders(order_id, item_count, paid, amount, is_test, created_at) "
            "VALUES(?,?,1,?,1,?)",
            (order_id, int(data.itemCount or 0), effective_price(), now_iso()),
        )
        conn.commit()
    return {"ok": True, "order_id": order_id}


# ------------------------------------------------------------- Админка --------
class AdminLoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    price: Optional[str] = None
    test_mode: Optional[bool] = None


def require_admin(request: Request):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Админка не настроена: задайте ADMIN_PASSWORD")
    if request.headers.get("x-admin-token", "") != ADMIN_TOKEN:
        raise HTTPException(401, "Требуется вход администратора")


@app.get("/admin")
def admin_page():
    return _page("admin.html")


@app.post("/api/admin/login")
def admin_login(data: AdminLoginIn):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Задайте переменную ADMIN_PASSWORD")
    if data.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Неверный пароль")
    return {"token": ADMIN_TOKEN}


def _all_orders():
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC").fetchall()]


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    require_admin(request)
    orders = _all_orders()
    paid = [o for o in orders if o["paid"]]
    real_paid = [o for o in paid if not o["is_test"]]
    revenue = sum(float(o["amount"] or 0) for o in real_paid)
    return {
        "orders": len(orders),
        "paidOrders": len(paid),
        "revenue": round(revenue, 2),
        "currency": CURRENCY,
        "price": effective_price(),
        "testMode": test_mode(),
        "paymentsReady": payments_ready(),
        "tryonEnabled": tryon_enabled(),
        "mannequins": len(available_mannequins()),
    }


@app.get("/api/admin/orders")
def admin_orders(request: Request):
    require_admin(request)
    return {"orders": _all_orders()[:200]}


@app.post("/api/admin/settings")
def admin_settings(request: Request, data: SettingsIn):
    require_admin(request)
    if data.price is not None:
        try:
            val = float(str(data.price).replace(",", "."))
            set_setting("price", f"{val:.2f}")
        except ValueError:
            raise HTTPException(400, "Некорректная цена")
    if data.test_mode is not None:
        set_setting("test_mode", "1" if data.test_mode else "0")
    return {"ok": True, "price": effective_price(), "testMode": test_mode()}


# -------------------------------------------------------------- Вебхук --------
@app.post("/api/yookassa/webhook")
async def yookassa_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)
    if (body or {}).get("event") == "payment.succeeded":
        order_id = ((body.get("object") or {}).get("metadata") or {}).get("order_id")
        if order_id:
            order = get_order(order_id)
            if order:
                verify_and_mark(order)  # источник истины — проверка через API
    return Response(status_code=200)
