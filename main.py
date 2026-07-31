"""
StyleGlobe для селлеров — каталожные фото/видео для Wildberries / Ozon.

Модель:
  1) Селлер загружает фото товара, выбирает модель, жмёт «Бесплатное превью»
     (1 бесплатное превью на посетителя, с прозрачным водяным знаком).
  2) Покупает ПАКЕТ генераций (1 / 10 / 25+видео / 50) через ЮKassa.
  3) После оплаты получает КОД ДОСТУПА с балансом генераций.
  4) По коду генерирует товары в HD без водяного знака — каждая генерация
     списывает 1 генерацию с баланса. Баланс хранится в ПОСТОЯННОЙ базе (PostgreSQL).

Идентификация — по коду доступа (без регистрации). Баланс — в БД.
Секреты только в переменных окружения.
"""

import os
import json
import uuid
import base64
import time
import secrets
import threading
import tempfile
from contextlib import closing

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from typing import Optional, List

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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_TOKEN = uuid.uuid4().hex

WAVESPEED_API_KEY = os.getenv("WAVESPEED_API_KEY", "").strip()
WAVESPEED_VIDEO_MODEL = os.getenv("WAVESPEED_VIDEO_MODEL", "wavespeed-ai/ai-virtual-outfit-tryon").strip()
WAVESPEED_PHOTO_MODEL = os.getenv("WAVESPEED_PHOTO_MODEL", "wavespeed-ai/ai-clothes-changer").strip()
WAVESPEED_BASE = os.getenv("WAVESPEED_BASE", "https://api.wavespeed.ai/api/v3").rstrip("/")
TRYON_DURATION = int(os.getenv("TRYON_DURATION", "5"))
TRYON_SEND_DURATION = os.getenv("TRYON_SEND_DURATION", "0") == "1"
TRYON_PROMPT = os.getenv("TRYON_PROMPT", "").strip()

# Пакеты (цены и число генераций). Менять можно здесь.
PACKAGES = [
    {"id": "test",    "title": "1 фото (Тест)",               "count": 1,  "videos": 0,  "price": "99.00",   "video": False},
    {"id": "start",   "title": "10 фото (Старт)",             "count": 10, "videos": 0,  "price": "990.00",  "video": False},
    {"id": "catalog", "title": "25 фото + 10 видео (Каталог)", "count": 25, "videos": 10, "price": "2490.00", "video": True},
    {"id": "pro",     "title": "50 фото + 30 видео (Профи)",   "count": 50, "videos": 30, "price": "3990.00", "video": True},
]
PACKAGE_BY_ID = {p["id"]: p for p in PACKAGES}

if _YK and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(tempfile.gettempdir(), "seller_imgs")
os.makedirs(IMG_DIR, exist_ok=True)
TASKS = {}

# ------------------------------------------------------- База данных (PG/SQLite)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    SQLITE_PATH = os.getenv("SQLITE_PATH") or os.path.join(tempfile.gettempdir(), "seller.db")


def _conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL)
    c = sqlite3.connect(SQLITE_PATH)
    c.row_factory = sqlite3.Row
    return c


def dbrun(sql, params=(), fetch=None):
    """Выполняет запрос. fetch: None | 'one' | 'all'. Плейсхолдеры пишем '?'."""
    q = sql.replace("?", "%s") if USE_PG else sql
    with closing(_conn()) as conn:
        if USE_PG:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(q, params)
        out = None
        if fetch == "one":
            row = cur.fetchone()
            out = dict(row) if row else None
        elif fetch == "all":
            out = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return out


def init_db():
    try:
        dbrun("""CREATE TABLE IF NOT EXISTS codes(
                    code TEXT PRIMARY KEY,
                    package TEXT,
                    credits_total INTEGER NOT NULL DEFAULT 0,
                    credits_left INTEGER NOT NULL DEFAULT 0,
                    has_video INTEGER NOT NULL DEFAULT 0,
                    video_total INTEGER NOT NULL DEFAULT 0,
                    video_left INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT )""")
        # миграция для уже созданной таблицы (добавляем видео-колонки, если их нет)
        for col in ("video_total", "video_left"):
            try:
                dbrun(f"ALTER TABLE codes ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
        dbrun("""CREATE TABLE IF NOT EXISTS orders(
                    order_id TEXT PRIMARY KEY,
                    package TEXT,
                    amount TEXT,
                    paid INTEGER NOT NULL DEFAULT 0,
                    is_test INTEGER NOT NULL DEFAULT 0,
                    payment_id TEXT,
                    code TEXT,
                    created_at TEXT )""")
        dbrun("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)""")
        dbrun("""CREATE TABLE IF NOT EXISTS free_usage(visitor_id TEXT PRIMARY KEY, created_at TEXT)""")
    except Exception as e:
        print("init_db warning:", e)


init_db()


def get_setting(key, default=None):
    try:
        row = dbrun("SELECT value FROM settings WHERE key=?", (key,), "one")
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key, value):
    if dbrun("SELECT 1 FROM settings WHERE key=?", (key,), "one"):
        dbrun("UPDATE settings SET value=? WHERE key=?", (str(value), key))
    else:
        dbrun("INSERT INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def test_mode():
    return (get_setting("test_mode") or "0") == "1"


def payments_ready():
    return bool(_YK and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def effective_payments_ready():
    return payments_ready() and not test_mode()


# ------------------------------------------------- Коды доступа / баланс -------
def new_code():
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # без похожих символов
    part = lambda: "".join(secrets.choice(alpha) for _ in range(4))
    return f"SG-{part()}-{part()}"


def create_code(package, count, videos):
    code = new_code()
    videos = int(videos or 0)
    dbrun("INSERT INTO codes(code,package,credits_total,credits_left,has_video,video_total,video_left,created_at) "
          "VALUES(?,?,?,?,?,?,?,?)",
          (code, package, count, count, 1 if videos > 0 else 0, videos, videos, now_iso()))
    return code


def get_code(code):
    code = (code or "").strip().upper()
    return dbrun("SELECT * FROM codes WHERE code=?", (code,), "one")


def spend_credit(code):
    """Атомарно списывает 1 фото-генерацию, если баланс > 0. Возвращает остаток или None."""
    code = (code or "").strip().upper()
    dbrun("UPDATE codes SET credits_left = credits_left - 1 WHERE code=? AND credits_left > 0", (code,))
    row = get_code(code)
    return row["credits_left"] if row else None


def spend_video(code):
    """Атомарно списывает 1 видео-генерацию, если видео-баланс > 0. Возвращает остаток или None."""
    code = (code or "").strip().upper()
    dbrun("UPDATE codes SET video_left = video_left - 1 WHERE code=? AND video_left > 0", (code,))
    row = get_code(code)
    return row.get("video_left") if row else None


# ---------------------------------------------------------------- Манекены ----
def mannequin_frame(n):
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = os.path.join(HERE, f"model-{n}.{ext}")
        if os.path.exists(p):
            return p
    return None


def available_mannequins():
    return [{"id": n} for n in (1, 2, 3) if mannequin_frame(n)]


def tryon_enabled():
    return bool(WAVESPEED_API_KEY) and len(available_mannequins()) > 0


# -------------------------------------------------------------- Приложение ----
app = FastAPI(title="StyleGlobe Sellers")
app.add_middleware(CORSMiddleware, allow_origins=[SITE_URL, "*"],
                   allow_methods=["*"], allow_headers=["*"])


def _page(name):
    p = os.path.join(HERE, name)
    return FileResponse(p) if os.path.exists(p) else JSONResponse({"detail": f"{name} не найден"}, status_code=404)


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


@app.get("/admin")
def p_admin():
    return _page("admin.html")


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok", "db": "postgres" if USE_PG else "sqlite"}


@app.get("/model/{n}")
def model_frame(n: int):
    p = mannequin_frame(n)
    if p:
        return FileResponse(p)
    raise HTTPException(404, "манекен не найден")


@app.get("/assets/{fname}")
def asset_file(fname: str):
    p = os.path.join(HERE, "assets", os.path.basename(fname))
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(404, "не найдено")


@app.get("/api/img/{token}")
def serve_img(token: str):
    safe = "".join(c for c in token if c.isalnum())
    p = os.path.join(IMG_DIR, safe + ".jpg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(404, "not found")


@app.get("/api/config")
def config():
    return {
        "currency": CURRENCY,
        "packages": PACKAGES,
        "paymentsEnabled": effective_payments_ready(),
        "tryonEnabled": tryon_enabled(),
        "mannequins": available_mannequins(),
        "maxItems": 8,
    }


# ---------------------------------------------------- Изображения/водяной знак -
def _save_dataurl(data_url):
    b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
    raw = base64.b64decode(b64)
    token = uuid.uuid4().hex
    with open(os.path.join(IMG_DIR, token + ".jpg"), "wb") as f:
        f.write(raw)
    return f"{SITE_URL}/api/img/{token}"


def _save_garment_urls(garments):
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
    return urls


def _download_to(url, path):
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def _wm_font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def make_transparent_watermark(src_path, dst_path):
    """Прозрачный водяной знак «плиткой» по всей картинке. Картинка остаётся видна
    (селлер оценивает ткань/складки), но использовать как готовое фото нельзя."""
    from PIL import Image, ImageDraw
    img = Image.open(src_path).convert("RGBA")
    W, H = img.size
    fs = max(20, W // 18)
    font = _wm_font(fs)
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    text = "StyleGlobe • превью"
    step_x = int(fs * 12)
    step_y = int(fs * 3.4)
    stroke = max(1, fs // 16)
    y = -H
    row = 0
    while y < H * 2:
        x = -W + (row % 2) * (step_x // 2)
        while x < W * 2:
            td.text((x, y), text, font=font, fill=(255, 255, 255, 105),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 105))
            x += step_x
        y += step_y
        row += 1
    tile = tile.rotate(30, expand=False)
    img = Image.alpha_composite(img, tile)
    img.convert("RGB").save(dst_path, "JPEG", quality=88)


# --------------------------------------------------------- WaveSpeed вызовы ----
def _wavespeed_create(portrait_url, clothes_urls, model, with_duration):
    url = f"{WAVESPEED_BASE}/{model}"
    body = {"image": portrait_url, "clothes_images": clothes_urls}
    if with_duration:
        body["duration"] = TRYON_DURATION
    if TRYON_PROMPT:
        body["prompt"] = TRYON_PROMPT
    r = requests.post(url, headers={"Authorization": f"Bearer {WAVESPEED_API_KEY}",
                                    "Content-Type": "application/json"}, json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"WaveSpeed {r.status_code}: {r.text[:400]}")
    j = r.json()
    data = j.get("data", j)
    pred_id = data.get("id") or j.get("id")
    poll_url = (data.get("urls", {}) or {}).get("get") if isinstance(data.get("urls"), dict) else None
    if not poll_url and pred_id:
        poll_url = f"{WAVESPEED_BASE}/predictions/{pred_id}/result"
    return pred_id, poll_url


def _wavespeed_poll(poll_url):
    r = requests.get(poll_url, headers={"Authorization": f"Bearer {WAVESPEED_API_KEY}"}, timeout=60)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", j)
    status = (data.get("status") or j.get("status") or "").lower()
    out = data.get("outputs") or data.get("output") or j.get("output")
    url = out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
    return status, url


def _generate(portrait_url, clothes, kind):
    is_video = (kind == "video")
    model = WAVESPEED_VIDEO_MODEL if is_video else WAVESPEED_PHOTO_MODEL
    pred_id, poll = _wavespeed_create(portrait_url, clothes, model, is_video and TRYON_SEND_DURATION)
    if not poll:
        raise RuntimeError("WaveSpeed: не получен идентификатор задачи")
    for _ in range(100 if is_video else 40):
        time.sleep(3)
        status, url = _wavespeed_poll(poll)
        if status in ("completed", "succeeded", "success") and url:
            return url
        if status in ("failed", "error", "canceled"):
            raise RuntimeError(f"WaveSpeed вернул статус: {status}")
    raise RuntimeError("превышено время ожидания результата")


# --------------------------------------------- Бесплатное превью (1 на гостя) --
def has_used_free(vid):
    if not vid:
        return False
    try:
        return dbrun("SELECT 1 FROM free_usage WHERE visitor_id=?", (vid,), "one") is not None
    except Exception:
        return False


def mark_free_used(vid):
    if not vid:
        return
    try:
        if not dbrun("SELECT 1 FROM free_usage WHERE visitor_id=?", (vid,), "one"):
            dbrun("INSERT INTO free_usage(visitor_id,created_at) VALUES(?,?)", (vid, now_iso()))
    except Exception as e:
        print("mark_free_used warn:", e)


class FreePreviewIn(BaseModel):
    mannequin: int = 1
    garments: List[str] = []
    visitorId: str = ""


def _run_free_preview(task_id, token, mannequin, clothes, vid):
    TASKS[task_id] = {"status": "processing", "token": token, "error": None}
    try:
        portrait = f"{SITE_URL}/model/{mannequin}"
        url = _generate(portrait, clothes, "photo")
        hd = os.path.join(IMG_DIR, f"prev_{token}.jpg")
        _download_to(url, hd)
        make_transparent_watermark(hd, os.path.join(IMG_DIR, f"prevwm_{token}.jpg"))
        mark_free_used(vid)
        TASKS[task_id] = {"status": "done", "token": token, "error": None,
                          "previewUrl": f"/api/preview/{token}"}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "token": token, "error": str(e)}


@app.post("/api/free-preview")
def free_preview(data: FreePreviewIn):
    if not tryon_enabled():
        raise HTTPException(503, "Генерация не настроена (нет ключа WaveSpeed или манекенов)")
    vid = (data.visitorId or "").strip()[:64]
    if vid and has_used_free(vid):
        raise HTTPException(429, "Бесплатное превью уже использовано. Купите пакет, чтобы создавать фото в HD.")
    clothes = _save_garment_urls(data.garments)
    if not clothes:
        raise HTTPException(400, "Нет фото товара")
    mannequin = data.mannequin if mannequin_frame(data.mannequin) else (
        available_mannequins()[0]["id"] if available_mannequins() else 1)
    token = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"status": "processing", "token": token, "error": None}
    threading.Thread(target=_run_free_preview, args=(task_id, token, mannequin, clothes, vid),
                     daemon=True).start()
    return {"task_id": task_id, "token": token}


@app.get("/api/preview/{token}")
def serve_preview(token: str):
    safe = "".join(c for c in token if c.isalnum())
    p = os.path.join(IMG_DIR, f"prevwm_{safe}.jpg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(404, "не найдено")


@app.get("/api/task/{task_id}")
def task_status(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    return t


# ---------------------------------------------------------- Покупка пакета ----
class CreatePaymentIn(BaseModel):
    package: str


@app.post("/api/create-payment")
def create_payment(data: CreatePaymentIn):
    if not effective_payments_ready():
        raise HTTPException(503, "Платежи не настроены или включён тестовый режим")
    pkg = PACKAGE_BY_ID.get(data.package)
    if not pkg:
        raise HTTPException(400, "Неизвестный пакет")
    order_id = uuid.uuid4().hex
    dbrun("INSERT INTO orders(order_id,package,amount,paid,is_test,created_at) VALUES(?,?,?,0,0,?)",
          (order_id, pkg["id"], pkg["price"], now_iso()))
    payment = Payment.create({
        "amount": {"value": pkg["price"], "currency": CURRENCY},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": f"{SITE_URL}/?order_id={order_id}"},
        "description": f"StyleGlobe — пакет «{pkg['title']}» ({pkg['count']} генераций)",
        "metadata": {"order_id": order_id},
    }, uuid.uuid4().hex)
    dbrun("UPDATE orders SET payment_id=? WHERE order_id=?", (payment.id, order_id))
    return {"order_id": order_id, "confirmation_url": payment.confirmation.confirmation_url}


def _issue_code_for_order(order):
    if order.get("code"):
        return order["code"]
    pkg = PACKAGE_BY_ID.get(order["package"])
    if not pkg:
        return None
    code = create_code(pkg["id"], pkg["count"], pkg["videos"])
    dbrun("UPDATE orders SET code=? WHERE order_id=?", (code, order["order_id"]))
    return code


def verify_and_issue(order):
    if order["paid"]:
        return True, (order.get("code") or _issue_code_for_order(order))
    if not (payments_ready() and order.get("payment_id")):
        return False, None
    try:
        p = Payment.find_one(order["payment_id"])
        if getattr(p, "status", None) == "succeeded":
            dbrun("UPDATE orders SET paid=1 WHERE order_id=?", (order["order_id"],))
            order["paid"] = 1
            return True, _issue_code_for_order(order)
    except Exception:
        return False, None
    return False, None


@app.get("/api/order/{order_id}")
def order_status(order_id: str):
    order = dbrun("SELECT * FROM orders WHERE order_id=?", (order_id,), "one")
    if not order:
        raise HTTPException(404, "Заказ не найден")
    paid, code = verify_and_issue(order)
    return {"paid": bool(paid), "code": code}


@app.get("/api/code/{code}")
def code_info(code: str):
    row = get_code(code)
    if not row:
        return {"valid": False}
    return {"valid": True, "creditsLeft": row["credits_left"], "creditsTotal": row["credits_total"],
            "videoLeft": int(row.get("video_left") or 0), "videoTotal": int(row.get("video_total") or 0),
            "hasVideo": int(row.get("video_left") or 0) > 0, "package": row["package"]}


# ------------------------------------------- Генерация в HD (списание кредита) -
class GenerateIn(BaseModel):
    code: str
    mannequin: int = 1
    kind: str = "photo"
    garments: List[str] = []


def _run_hd(task_id, token, mannequin, clothes, kind):
    TASKS[task_id] = {"status": "processing", "token": token, "kind": kind, "error": None}
    try:
        portrait = f"{SITE_URL}/model/{mannequin}"
        url = _generate(portrait, clothes, kind)
        if kind == "photo":
            _download_to(url, os.path.join(IMG_DIR, f"hd_{token}.jpg"))
            TASKS[task_id] = {"status": "done", "token": token, "kind": "photo",
                              "error": None, "url": f"/api/hd/{token}.jpg"}
        else:
            TASKS[task_id] = {"status": "done", "token": token, "kind": "video",
                              "error": None, "url": url}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "token": token, "kind": kind, "error": str(e)}


@app.get("/api/hd/{name}")
def serve_hd(name: str):
    safe = "".join(c for c in name.replace(".jpg", "") if c.isalnum())
    p = os.path.join(IMG_DIR, f"hd_{safe}.jpg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/jpeg", filename="styleglobe-hd.jpg")
    raise HTTPException(404, "не найдено")


@app.post("/api/generate")
def generate_hd(data: GenerateIn):
    row = get_code(data.code)
    if not row:
        raise HTTPException(404, "Код не найден")
    kind = "video" if str(data.kind).lower() == "video" else "photo"
    if not tryon_enabled():
        raise HTTPException(503, "Генерация не настроена")
    if kind == "video":
        if int(row.get("video_left") or 0) <= 0:
            raise HTTPException(402, "Видео в этом пакете закончились или не входят.")
    else:
        if row["credits_left"] <= 0:
            raise HTTPException(402, "Фото-генерации закончились. Купите новый пакет.")
    clothes = _save_garment_urls(data.garments)
    if not clothes:
        raise HTTPException(400, "Нет фото товара")
    left = spend_video(data.code) if kind == "video" else spend_credit(data.code)  # списываем ДО запуска
    if left is None:
        raise HTTPException(402, "Генерации закончились")
    mannequin = data.mannequin if mannequin_frame(data.mannequin) else (
        available_mannequins()[0]["id"] if available_mannequins() else 1)
    token = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"status": "processing", "token": token, "kind": kind, "error": None}
    threading.Thread(target=_run_hd, args=(task_id, token, mannequin, clothes, kind),
                     daemon=True).start()
    fresh = get_code(data.code) or {}
    return {"task_id": task_id, "kind": kind, "creditsLeft": left,
            "photoLeft": int(fresh.get("credits_left") or 0),
            "videoLeft": int(fresh.get("video_left") or 0)}


# ------------------------------------------------------------- Тест-заказ -----
class TestOrderIn(BaseModel):
    package: str


@app.post("/api/test-order")
def test_order(data: TestOrderIn):
    if effective_payments_ready():
        raise HTTPException(403, "Недоступно в боевом режиме")
    pkg = PACKAGE_BY_ID.get(data.package)
    if not pkg:
        raise HTTPException(400, "Неизвестный пакет")
    order_id = uuid.uuid4().hex
    code = create_code(pkg["id"], pkg["count"], pkg["videos"])
    dbrun("INSERT INTO orders(order_id,package,amount,paid,is_test,code,created_at) "
          "VALUES(?,?,?,1,1,?,?)", (order_id, pkg["id"], pkg["price"], code, now_iso()))
    return {"ok": True, "order_id": order_id, "code": code}


# ------------------------------------------------------------- Админка --------
class AdminLoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    test_mode: Optional[bool] = None


def require_admin(request: Request):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Админка не настроена: задайте ADMIN_PASSWORD")
    if request.headers.get("x-admin-token", "") != ADMIN_TOKEN:
        raise HTTPException(401, "Требуется вход администратора")


@app.post("/api/admin/login")
def admin_login(data: AdminLoginIn):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Задайте ADMIN_PASSWORD")
    if data.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Неверный пароль")
    return {"token": ADMIN_TOKEN}


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    require_admin(request)
    orders = dbrun("SELECT * FROM orders ORDER BY created_at DESC", (), "all") or []
    real_paid = [o for o in orders if o["paid"] and not o["is_test"]]
    revenue = sum(float(o["amount"] or 0) for o in real_paid)
    codes = dbrun("SELECT * FROM codes", (), "all") or []
    return {"orders": len(orders), "paidOrders": len([o for o in orders if o["paid"]]),
            "revenue": round(revenue, 2), "currency": CURRENCY, "testMode": test_mode(),
            "paymentsReady": payments_ready(), "tryonEnabled": tryon_enabled(),
            "mannequins": len(available_mannequins()), "codes": len(codes),
            "creditsLeft": sum(c["credits_left"] for c in codes),
            "videoLeft": sum(int(c.get("video_left") or 0) for c in codes),
            "db": "postgres" if USE_PG else "sqlite (ВРЕМЕННАЯ — задайте DATABASE_URL!)"}


@app.get("/api/admin/orders")
def admin_orders(request: Request):
    require_admin(request)
    return {"orders": (dbrun("SELECT * FROM orders ORDER BY created_at DESC", (), "all") or [])[:200]}


@app.post("/api/admin/settings")
def admin_settings(request: Request, data: SettingsIn):
    require_admin(request)
    if data.test_mode is not None:
        set_setting("test_mode", "1" if data.test_mode else "0")
    return {"ok": True, "testMode": test_mode()}


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
            order = dbrun("SELECT * FROM orders WHERE order_id=?", (order_id,), "one")
            if order:
                verify_and_issue(order)
    return Response(status_code=200)
