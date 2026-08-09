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
import hashlib
import hmac
import io
import re
from contextlib import closing
from decimal import Decimal, InvalidOperation

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
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
ENABLE_TEST_ORDERS = os.getenv("ENABLE_TEST_ORDERS", "0") == "1"
TEST_ORDER_SECRET = os.getenv("TEST_ORDER_SECRET", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
if APP_ENV == "production" and len(SESSION_SECRET.encode()) < 32:
    raise RuntimeError("SESSION_SECRET must contain at least 32 bytes in production")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("security warning: SESSION_SECRET is not configured; sessions reset on restart")
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "1") == "1"
FREE_PREVIEW_IP_LIMIT = max(1, int(os.getenv("FREE_PREVIEW_IP_LIMIT", "3")))
MAX_REQUEST_BYTES = max(1024 * 1024, int(os.getenv("MAX_REQUEST_BYTES", str(12 * 1024 * 1024))))
MAX_IMAGE_BYTES = max(256 * 1024, int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024))))
MAX_IMAGE_PIXELS = max(1_000_000, int(os.getenv("MAX_IMAGE_PIXELS", "20000000")))
MAX_IMAGE_EDGE = max(512, int(os.getenv("MAX_IMAGE_EDGE", "4096")))
FILE_TTL_HOURS = max(1, int(os.getenv("FILE_TTL_HOURS", "24")))
ADMIN_SESSION_HOURS = min(24 * 7, max(1, int(os.getenv("ADMIN_SESSION_HOURS", "12"))))
METRIKA_ID = os.getenv("METRIKA_ID", "").strip()   # номер счётчика Яндекс.Метрики

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
LAST_FILE_CLEANUP = 0.0
FILE_CLEANUP_LOCK = threading.Lock()

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


def _column_exists(table, column):
    if USE_PG:
        row = dbrun(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=? AND column_name=?",
            (table, column), "one"
        )
        return bool(row)
    rows = dbrun(f"PRAGMA table_info({table})", (), "all") or []
    return any(row.get("name") == column for row in rows)


def _migration_done(version):
    return bool(dbrun("SELECT 1 FROM schema_migrations WHERE version=?", (version,), "one"))


def _mark_migration(version):
    dbrun("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, time.time()))


def apply_migrations():
    if not _migration_done(1):
        for column in ("video_total", "video_left"):
            if not _column_exists("codes", column):
                dbrun(f"ALTER TABLE codes ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if not _column_exists("codes", "order_id"):
            dbrun("ALTER TABLE codes ADD COLUMN order_id TEXT")
        dbrun("CREATE UNIQUE INDEX IF NOT EXISTS idx_codes_order_id ON codes(order_id)")
        dbrun("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id)")
        _mark_migration(1)

    if not _migration_done(2):
        dbrun("""CREATE TABLE IF NOT EXISTS free_preview_usage(
                    session_hash TEXT PRIMARY KEY,
                    ip_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL )""")
        dbrun("CREATE INDEX IF NOT EXISTS idx_free_preview_ip ON free_preview_usage(ip_hash,created_at)")
        dbrun("""CREATE TABLE IF NOT EXISTS generation_jobs(
                    job_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    error TEXT )""")
        _mark_migration(2)

    if not _migration_done(3):
        dbrun("""CREATE TABLE IF NOT EXISTS admin_sessions(
                    session_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    ip_hash TEXT NOT NULL,
                    user_agent_hash TEXT NOT NULL )""")
        dbrun("CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at)")
        dbrun("""CREATE TABLE IF NOT EXISTS rate_limits(
                    bucket_key TEXT NOT NULL,
                    window_start BIGINT NOT NULL,
                    hits INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(bucket_key,window_start) )""")
        dbrun("CREATE INDEX IF NOT EXISTS idx_rate_limits_updated ON rate_limits(updated_at)")
        _mark_migration(3)


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
        dbrun("""CREATE TABLE IF NOT EXISTS feedback(
                    id TEXT PRIMARY KEY,
                    reason TEXT,
                    comment TEXT,
                    visitor_id TEXT,
                    created_at TEXT )""")
        dbrun("""CREATE TABLE IF NOT EXISTS schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL )""")
        apply_migrations()
    except Exception as exc:
        print("init_db failed:", exc)
        if APP_ENV == "production":
            raise


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


def _begin(conn):
    if USE_PG:
        conn.autocommit = False
    else:
        conn.execute("BEGIN IMMEDIATE")


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_PG else conn.cursor()


def _sql(sql):
    return sql.replace("?", "%s") if USE_PG else sql


def _row_dict(row, cursor=None):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return dict(row)
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def test_mode():
    return APP_ENV != "production" and (get_setting("test_mode") or "0") == "1"


def payments_ready():
    return bool(_YK and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def effective_payments_ready():
    return payments_ready() and not test_mode()


def _payment_field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def payment_matches_order(payment, order):
    """Accept only the exact YooKassa payment created for this order."""
    if not payment or _payment_field(payment, "status") != "succeeded":
        return False
    if _payment_field(payment, "paid") is not True:
        return False
    if str(_payment_field(payment, "id", "")) != str(order.get("payment_id") or ""):
        return False

    metadata = _payment_field(payment, "metadata", {}) or {}
    if str(_payment_field(metadata, "order_id", "")) != str(order.get("order_id") or ""):
        return False

    amount = _payment_field(payment, "amount", {}) or {}
    if str(_payment_field(amount, "currency", "")).upper() != CURRENCY.upper():
        return False
    try:
        actual = Decimal(str(_payment_field(amount, "value", ""))).quantize(Decimal("0.01"))
        expected = Decimal(str(order.get("amount") or "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return False
    return actual == expected


# ------------------------------------------------- Коды доступа / баланс -------
def new_code():
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # без похожих символов
    part = lambda: "".join(secrets.choice(alpha) for _ in range(4))
    return f"SG-{part()}-{part()}"


def create_code(package, count, videos, order_id=None):
    code = new_code()
    videos = int(videos or 0)
    dbrun("INSERT INTO codes(code,package,credits_total,credits_left,has_video,video_total,video_left,created_at,order_id) "
          "VALUES(?,?,?,?,?,?,?,?,?)",
          (code, package, count, count, 1 if videos > 0 else 0, videos, videos, now_iso(), order_id))
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


def reserve_generation(code, kind, job_id):
    """Atomically reserves one credit and creates a durable job record."""
    code = (code or "").strip().upper()
    column = "video_left" if kind == "video" else "credits_left"
    with closing(_conn()) as conn:
        _begin(conn)
        cur = _cursor(conn)
        cur.execute(_sql(
            f"UPDATE codes SET {column}={column}-1 WHERE code=? AND {column}>0 "
            "RETURNING credits_left,video_left"
        ), (code,))
        row = _row_dict(cur.fetchone(), cur)
        if not row:
            conn.rollback()
            return None
        cur.execute(_sql("INSERT INTO generation_jobs(job_id,code,kind,status,created_at) VALUES(?,?,?,?,?)"),
                    (job_id, code, kind, "processing", time.time()))
        conn.commit()
        return {"credits_left": int(row["credits_left"]), "video_left": int(row["video_left"])}


def finish_generation(job_id, succeeded, error=None):
    """Finalizes a reservation and refunds it exactly once after failure."""
    with closing(_conn()) as conn:
        _begin(conn)
        cur = _cursor(conn)
        lock = " FOR UPDATE" if USE_PG else ""
        cur.execute(_sql("SELECT * FROM generation_jobs WHERE job_id=?" + lock), (job_id,))
        job = _row_dict(cur.fetchone(), cur)
        if not job or job["status"] != "processing":
            conn.rollback()
            return False
        if succeeded:
            cur.execute(_sql("UPDATE generation_jobs SET status='succeeded',finished_at=? WHERE job_id=?"),
                        (time.time(), job_id))
        else:
            column = "video_left" if job["kind"] == "video" else "credits_left"
            cur.execute(_sql(f"UPDATE codes SET {column}={column}+1 WHERE code=?"), (job["code"],))
            cur.execute(_sql("UPDATE generation_jobs SET status='refunded',finished_at=?,error=? WHERE job_id=?"),
                        (time.time(), str(error or "generation failed")[:500], job_id))
        conn.commit()
        return True


def recover_interrupted_generations():
    """Refunds jobs left by a previous single-process server instance."""
    rows = dbrun("SELECT job_id FROM generation_jobs WHERE status='processing'", (), "all") or []
    recovered = 0
    for row in rows:
        if finish_generation(row["job_id"], False, "server restarted before completion"):
            recovered += 1
    return recovered


RECOVERED_GENERATIONS = recover_interrupted_generations()


# ---------------------------------------------------------------- Манекены ----
def mannequin_frame(n):
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = os.path.join(HERE, f"model-{n}.{ext}")
        if os.path.exists(p):
            return p
    return None


def available_mannequins():
    return [{"id": n} for n in range(1, 9) if mannequin_frame(n)]


def tryon_enabled():
    return bool(WAVESPEED_API_KEY) and len(available_mannequins()) > 0


# -------------------------------------------------------------- Приложение ----
app = FastAPI(title="StyleGlobe Sellers")
app.add_middleware(CORSMiddleware, allow_origins=[SITE_URL],
                   allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-CSRF-Token", "X-Test-Order-Secret"])


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "invalid content length"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/admin"):
        response.headers["Cache-Control"] = "no-store"
    if not request.cookies.get("sg_session"):
        session_id = secrets.token_hex(16)
        response.set_cookie("sg_session", _signed_session_value(session_id), max_age=31536000,
                            httponly=True, secure=SITE_URL.startswith("https://"), samesite="lax")
    return response


def _page(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return JSONResponse({"detail": f"{name} не найден"}, status_code=404)
    if name.endswith(".html"):
        try:
            html = open(p, encoding="utf-8").read()
            html = html.replace("__METRIKA_ID__", METRIKA_ID or "0")
            return Response(content=html, media_type="text/html")
        except Exception:
            return FileResponse(p)
    return FileResponse(p)


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


@app.get("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{SITE_URL}/</loc></url>\n'
        f'  <url><loc>{SITE_URL}/oferta</loc></url>\n'
        f'  <url><loc>{SITE_URL}/politika</loc></url>\n'
        f'  <url><loc>{SITE_URL}/kontakty</loc></url>\n'
        '</urlset>\n'
    )
    return Response(content=body, media_type="application/xml")


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
    from PIL import Image, ImageOps, UnidentifiedImageError

    cleanup_old_files()

    if not isinstance(data_url, str):
        raise ValueError("invalid image")
    match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\r\n]+)", data_url)
    if not match:
        raise ValueError("only JPEG, PNG and WEBP data URLs are accepted")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("invalid base64 image") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or too large")
    try:
        probe = Image.open(io.BytesIO(raw))
        if probe.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("unsupported image format")
        width, height = probe.size
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("invalid image dimensions")
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("image cannot be decoded") from exc
    token = uuid.uuid4().hex
    image.save(os.path.join(IMG_DIR, token + ".jpg"), "JPEG", quality=90, optimize=True)
    return f"{SITE_URL}/api/img/{token}"


def cleanup_old_files(force=False):
    global LAST_FILE_CLEANUP
    now = time.time()
    if not force and now - LAST_FILE_CLEANUP < 600:
        return 0
    removed = 0
    cutoff = now - FILE_TTL_HOURS * 3600
    with FILE_CLEANUP_LOCK:
        if not force and now - LAST_FILE_CLEANUP < 600:
            return 0
        for name in os.listdir(IMG_DIR):
            path = os.path.join(IMG_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        LAST_FILE_CLEANUP = now
    return removed


def _save_garment_urls(garments):
    urls = []
    for g in (garments or []):
        if not g:
            continue
        urls.append(_save_dataurl(g))
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
def _signed_session_value(session_id):
    signature = hmac.new(SESSION_SECRET.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}.{signature}"


def _session_id_from_request(request):
    raw = request.cookies.get("sg_session", "")
    try:
        session_id, signature = raw.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(SESSION_SECRET.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{32}", session_id) or not hmac.compare_digest(signature, expected):
        return None
    return session_id


def _client_ip(request):
    if TRUST_PROXY_HEADERS:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
        forwarded = [part for part in forwarded if part]
        if forwarded:
            # A single trusted edge proxy appends the actual peer address last.
            # Taking the first value would let a client prepend an arbitrary IP.
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def _private_hash(label, value):
    return hmac.new(SESSION_SECRET.encode(), f"{label}:{value}".encode(), hashlib.sha256).hexdigest()


def consume_rate_limit(namespace, subject, limit, window_seconds):
    """Atomically consumes a shared database-backed rate-limit slot."""
    now = time.time()
    window_start = int(now // window_seconds) * int(window_seconds)
    bucket_key = _private_hash("rate-limit", f"{namespace}:{subject}")
    with closing(_conn()) as conn:
        _begin(conn)
        cur = _cursor(conn)
        cur.execute(_sql("DELETE FROM rate_limits WHERE updated_at<?"), (now - 7 * 86400,))
        cur.execute(_sql(
            "INSERT INTO rate_limits(bucket_key,window_start,hits,updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(bucket_key,window_start) DO UPDATE SET "
            "hits=rate_limits.hits+1,updated_at=excluded.updated_at RETURNING hits"
        ), (bucket_key, window_start, now))
        row = _row_dict(cur.fetchone(), cur)
        conn.commit()
    hits = int(row["hits"])
    retry_after = max(1, window_start + int(window_seconds) - int(now))
    return {"allowed": hits <= int(limit), "hits": hits, "retry_after": retry_after}


def clear_rate_limit(namespace, subject):
    bucket_key = _private_hash("rate-limit", f"{namespace}:{subject}")
    dbrun("DELETE FROM rate_limits WHERE bucket_key=?", (bucket_key,))


def reserve_free_preview(session_hash, ip_hash):
    """Reserves a free preview before any external AI call."""
    with closing(_conn()) as conn:
        _begin(conn)
        cur = _cursor(conn)
        cur.execute(_sql("SELECT 1 FROM free_preview_usage WHERE session_hash=?"), (session_hash,))
        if cur.fetchone():
            conn.rollback()
            return False
        cutoff = time.time() - 86400
        cur.execute(_sql("SELECT COUNT(*) AS n FROM free_preview_usage WHERE ip_hash=? AND created_at>=?"),
                    (ip_hash, cutoff))
        count_row = _row_dict(cur.fetchone(), cur)
        if int(count_row["n"]) >= FREE_PREVIEW_IP_LIMIT:
            conn.rollback()
            return False
        now = time.time()
        cur.execute(_sql("INSERT INTO free_preview_usage(session_hash,ip_hash,status,created_at,updated_at) "
                         "VALUES(?,?,?,?,?)"), (session_hash, ip_hash, "processing", now, now))
        conn.commit()
        return True


def finish_free_preview(session_hash, succeeded):
    dbrun("UPDATE free_preview_usage SET status=?,updated_at=? WHERE session_hash=? AND status='processing'",
          ("used" if succeeded else "failed", time.time(), session_hash))


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


def _run_free_preview(task_id, token, mannequin, clothes, session_hash):
    TASKS[task_id] = {"status": "processing", "token": token, "error": None}
    try:
        portrait = f"{SITE_URL}/model/{mannequin}"
        url = _generate(portrait, clothes, "photo")
        hd = os.path.join(IMG_DIR, f"prev_{token}.jpg")
        _download_to(url, hd)
        make_transparent_watermark(hd, os.path.join(IMG_DIR, f"prevwm_{token}.jpg"))
        finish_free_preview(session_hash, True)
        TASKS[task_id] = {"status": "done", "token": token, "error": None,
                          "previewUrl": f"/api/preview/{token}"}
    except Exception as e:
        finish_free_preview(session_hash, False)
        TASKS[task_id] = {"status": "error", "token": token, "error": str(e)}


@app.post("/api/free-preview")
def free_preview(data: FreePreviewIn, request: Request):
    if not tryon_enabled():
        raise HTTPException(503, "Генерация не настроена (нет ключа WaveSpeed или манекенов)")
    try:
        clothes = _save_garment_urls(data.garments)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not clothes:
        raise HTTPException(400, "Нет фото товара")
    mannequin = data.mannequin if mannequin_frame(data.mannequin) else (
        available_mannequins()[0]["id"] if available_mannequins() else 1)
    session_id = _session_id_from_request(request) or secrets.token_hex(16)
    session_hash = _private_hash("session", session_id)
    ip_hash = _private_hash("ip", _client_ip(request))
    if not test_mode() and not reserve_free_preview(session_hash, ip_hash):
        raise HTTPException(429, "Бесплатное превью уже использовано или достигнут суточный лимит.")
    token = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"status": "processing", "token": token, "error": None}
    threading.Thread(target=_run_free_preview, args=(task_id, token, mannequin, clothes, session_hash),
                     daemon=True).start()
    response = JSONResponse({"task_id": task_id, "token": token})
    response.set_cookie("sg_session", _signed_session_value(session_id), max_age=31536000,
                        httponly=True, secure=SITE_URL.startswith("https://"), samesite="lax")
    return response


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
    try:
        payment = Payment.create({
            "amount": {"value": pkg["price"], "currency": CURRENCY},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": f"{SITE_URL}/?order_id={order_id}"},
            "description": f"StyleGlobe — пакет «{pkg['title']}» ({pkg['count']} генераций)",
            "metadata": {"order_id": order_id},
        }, order_id)
    except Exception:
        raise HTTPException(502, "ЮKassa временно недоступна; платёж не создан")
    confirmation = _payment_field(payment, "confirmation")
    confirmation_url = _payment_field(confirmation, "confirmation_url", "")
    if not _payment_field(payment, "id") or not confirmation_url:
        raise HTTPException(502, "ЮKassa вернула неполный ответ; платёж не создан")
    dbrun("UPDATE orders SET payment_id=? WHERE order_id=?", (payment.id, order_id))
    return {"order_id": order_id, "confirmation_url": confirmation_url}


def issue_code_for_order(order_id):
    """Returns the single access code linked to a paid order."""
    with closing(_conn()) as conn:
        _begin(conn)
        cur = _cursor(conn)
        lock = " FOR UPDATE" if USE_PG else ""
        cur.execute(_sql("SELECT * FROM orders WHERE order_id=?" + lock), (order_id,))
        order = _row_dict(cur.fetchone(), cur)
        if not order or not order["paid"]:
            conn.rollback()
            return None
        if order.get("code"):
            conn.commit()
            return order["code"]
        pkg = PACKAGE_BY_ID.get(order["package"])
        if not pkg:
            conn.rollback()
            return None
        code = new_code()
        videos = int(pkg["videos"] or 0)
        cur.execute(_sql(
            "INSERT INTO codes(code,package,credits_total,credits_left,has_video,video_total,video_left,created_at,order_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)"
        ), (code, pkg["id"], pkg["count"], pkg["count"], 1 if videos > 0 else 0,
            videos, videos, now_iso(), order_id))
        cur.execute(_sql("UPDATE orders SET code=? WHERE order_id=? AND code IS NULL"), (code, order_id))
        conn.commit()
        return code


def verify_and_issue(order):
    if order["paid"]:
        return True, (order.get("code") or issue_code_for_order(order["order_id"]))
    if not (payments_ready() and order.get("payment_id")):
        return False, None
    try:
        p = Payment.find_one(order["payment_id"])
        if payment_matches_order(p, order):
            dbrun("UPDATE orders SET paid=1 WHERE order_id=? AND paid=0", (order["order_id"],))
            order["paid"] = 1
            return True, issue_code_for_order(order["order_id"])
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
        finish_generation(task_id, True)
    except Exception as e:
        finish_generation(task_id, False, e)
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
    try:
        clothes = _save_garment_urls(data.garments)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not clothes:
        raise HTTPException(400, "Нет фото товара")
    mannequin = data.mannequin if mannequin_frame(data.mannequin) else (
        available_mannequins()[0]["id"] if available_mannequins() else 1)
    token = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    balances = reserve_generation(data.code, kind, task_id)
    if balances is None:
        raise HTTPException(402, "Генерации закончились")
    TASKS[task_id] = {"status": "processing", "token": token, "kind": kind, "error": None}
    threading.Thread(target=_run_hd, args=(task_id, token, mannequin, clothes, kind),
                     daemon=True).start()
    return {"task_id": task_id, "kind": kind,
            "creditsLeft": balances["credits_left"],
            "photoLeft": balances["credits_left"],
            "videoLeft": balances["video_left"]}


# -------------------------------------------------------------- Обратная связь -
class FeedbackIn(BaseModel):
    reason: str
    comment: Optional[str] = ""
    visitorId: Optional[str] = ""


FEEDBACK_REASONS = {
    "дорого",
    "не уверен в качестве",
    "не разобрался",
    "просто смотрю",
    "другое",
}


@app.post("/api/feedback")
def submit_feedback(data: FeedbackIn):
    reason = (data.reason or "").strip()[:60]
    if reason not in FEEDBACK_REASONS:
        raise HTTPException(400, "Недопустимая причина")
    comment = (data.comment or "").strip()[:500]
    vid = (data.visitorId or "").strip()[:80]
    dbrun("INSERT INTO feedback(id,reason,comment,visitor_id,created_at) VALUES(?,?,?,?,?)",
          (uuid.uuid4().hex, reason, comment, vid, now_iso()))
    return {"ok": True}


# ------------------------------------------------------------- Тест-заказ -----
class TestOrderIn(BaseModel):
    package: str


@app.post("/api/test-order")
def test_order(data: TestOrderIn, request: Request):
    supplied = request.headers.get("x-test-order-secret", "")
    allowed = (APP_ENV != "production" and ENABLE_TEST_ORDERS and TEST_ORDER_SECRET and
               hmac.compare_digest(supplied, TEST_ORDER_SECRET))
    if not allowed:
        raise HTTPException(404, "not found")
    pkg = PACKAGE_BY_ID.get(data.package)
    if not pkg:
        raise HTTPException(400, "Неизвестный пакет")
    order_id = uuid.uuid4().hex
    dbrun("INSERT INTO orders(order_id,package,amount,paid,is_test,code,created_at) "
          "VALUES(?,?,?,1,1,NULL,?)", (order_id, pkg["id"], pkg["price"], now_iso()))
    code = issue_code_for_order(order_id)
    return {"ok": True, "order_id": order_id, "code": code}


# ------------------------------------------------------------- Админка --------
class AdminLoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    test_mode: Optional[bool] = None


ADMIN_COOKIE_NAME = "sg_admin"


def _set_admin_cookie(response, token):
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=ADMIN_SESSION_HOURS * 3600,
        httponly=True,
        secure=SITE_URL.startswith("https://"),
        samesite="strict",
        path="/",
    )


def _create_admin_session(request):
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = time.time()
    dbrun("DELETE FROM admin_sessions WHERE expires_at<?", (now,))
    dbrun(
        "INSERT INTO admin_sessions(session_hash,csrf_hash,created_at,expires_at,last_seen,ip_hash,user_agent_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            _private_hash("admin-session", token),
            _private_hash("admin-csrf", csrf_token),
            now,
            now + ADMIN_SESSION_HOURS * 3600,
            now,
            _private_hash("admin-ip", _client_ip(request)),
            _private_hash("admin-user-agent", request.headers.get("user-agent", "")[:500]),
        ),
    )
    return token, csrf_token


def require_admin(request: Request, require_csrf=False):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Админка не настроена: задайте ADMIN_PASSWORD")
    token = request.cookies.get(ADMIN_COOKIE_NAME, "")
    session_hash = _private_hash("admin-session", token)
    session = dbrun("SELECT * FROM admin_sessions WHERE session_hash=?", (session_hash,), "one")
    now = time.time()
    if not session or float(session["expires_at"]) <= now:
        if session:
            dbrun("DELETE FROM admin_sessions WHERE session_hash=?", (session_hash,))
        raise HTTPException(401, "Требуется вход администратора")
    if require_csrf:
        supplied = request.headers.get("x-csrf-token", "")
        supplied_hash = _private_hash("admin-csrf", supplied)
        if not hmac.compare_digest(supplied_hash, session["csrf_hash"]):
            raise HTTPException(403, "Недействительный CSRF-токен")
    dbrun("UPDATE admin_sessions SET last_seen=? WHERE session_hash=?", (now, session_hash))
    return session


@app.post("/api/admin/login")
def admin_login(data: AdminLoginIn, request: Request, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Задайте ADMIN_PASSWORD")
    attempt_key = _private_hash("admin-login", _client_ip(request))
    rate = consume_rate_limit("admin-login", attempt_key, 5, 900)
    if not rate["allowed"]:
        raise HTTPException(429, "Слишком много попыток. Повторите позже.",
                            headers={"Retry-After": str(rate["retry_after"])})
    if not hmac.compare_digest(data.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Неверный пароль")
    clear_rate_limit("admin-login", attempt_key)
    token, csrf_token = _create_admin_session(request)
    _set_admin_cookie(response, token)
    return {"ok": True, "csrfToken": csrf_token, "expiresIn": ADMIN_SESSION_HOURS * 3600}


@app.get("/api/admin/session")
def admin_session(request: Request):
    session = require_admin(request)
    csrf_token = secrets.token_urlsafe(32)
    dbrun("UPDATE admin_sessions SET csrf_hash=? WHERE session_hash=?",
          (_private_hash("admin-csrf", csrf_token), session["session_hash"]))
    return {"ok": True, "csrfToken": csrf_token}


@app.post("/api/admin/logout")
def admin_logout(request: Request, response: Response):
    session = require_admin(request, require_csrf=True)
    dbrun("DELETE FROM admin_sessions WHERE session_hash=?", (session["session_hash"],))
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/", secure=SITE_URL.startswith("https://"),
                           httponly=True, samesite="strict")
    return {"ok": True}


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


@app.get("/api/admin/feedback")
def admin_feedback(request: Request):
    require_admin(request)
    rows = dbrun("SELECT * FROM feedback ORDER BY created_at DESC", (), "all") or []
    counts = {}
    for r in rows:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    return {"total": len(rows), "counts": counts, "items": rows[:200]}


@app.post("/api/admin/settings")
def admin_settings(request: Request, data: SettingsIn):
    require_admin(request, require_csrf=True)
    if data.test_mode is not None:
        if APP_ENV == "production" and data.test_mode:
            raise HTTPException(403, "Тестовый режим запрещён в production")
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
        payment_id = (body.get("object") or {}).get("id")
        if payment_id:
            order = dbrun("SELECT * FROM orders WHERE payment_id=?", (str(payment_id),), "one")
            if order:
                verify_and_issue(order)
    return Response(status_code=200)
