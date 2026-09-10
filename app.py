import os
import sqlite3
import hashlib
import binascii
import uuid
import datetime
from typing import List

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from translate import translate_all, LANGS
from i18n import t, FLAGS, NATIVE_NAME

# Let Pillow open iPhone HEIC/HEIF photos (no-op if lib missing)
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "market.db")
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "0") == "1",  # set 1 behind HTTPS
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def render(name, context):
    return templates.TemplateResponse(context["request"], name, context)

# ---------- helpers ----------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads while writing
    conn.execute("PRAGMA busy_timeout=5000")  # wait instead of instant 'locked'
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000)
    return binascii.hexlify(salt).decode() + "$" + binascii.hexlify(dk).decode()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), binascii.unhexlify(salt_hex), 100_000)
        return binascii.hexlify(dk).decode() == dk_hex
    except Exception:
        return False


def tier(qty: int, p1: float, p5: float, p10: float):
    """Price fields are LOT TOTALS: p5 = total for 5 pcs, p10 = total for 10 pcs.
    p1 = price of a single piece. Within a tier the per-piece rate is
    total/tier-size, so buying exactly 5 costs p5, exactly 10 costs p10,
    and in-between quantities are charged proportionally at that rate."""
    if qty >= 10:
        return p10 / 10.0, "10+"
    if qty >= 5:
        return p5 / 5.0, "5+"
    return p1, "1"


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS partner (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS product (
            id INTEGER PRIMARY KEY,
            partner_id INTEGER NOT NULL REFERENCES partner(id),
            base_name TEXT NOT NULL,
            base_desc TEXT DEFAULT '',
            price_1 REAL NOT NULL,
            price_5 REAL NOT NULL,
            price_10 REAL NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS product_tr (
            product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
            lang TEXT NOT NULL,
            name TEXT,
            description TEXT,
            PRIMARY KEY (product_id, lang)
        );
        CREATE TABLE IF NOT EXISTS product_photo (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            thumb TEXT,
            is_main INTEGER DEFAULT 0,
            sort INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS suborder (
            id INTEGER PRIMARY KEY,
            partner_id INTEGER NOT NULL REFERENCES partner(id),
            contact TEXT DEFAULT '',
            selfie TEXT,
            selfie_thumb TEXT,
            lang TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',   -- 'new' | 'deliver' | 'skip'
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orderline (
            id INTEGER PRIMARY KEY,
            suborder_id INTEGER NOT NULL REFERENCES suborder(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES product(id),
            qty INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            tier TEXT NOT NULL
        );
        """
    )
    # migration for DBs created before the thumb column existed
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(product_photo)")]
    if "thumb" not in cols:
        conn.execute("ALTER TABLE product_photo ADD COLUMN thumb TEXT")
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(suborder)")]
    if "selfie" not in scols:
        conn.execute("ALTER TABLE suborder ADD COLUMN selfie TEXT")
    if "selfie_thumb" not in scols:
        conn.execute("ALTER TABLE suborder ADD COLUMN selfie_thumb TEXT")
    conn.commit()
    conn.close()


def current_partner(request: Request):
    return request.session.get("partner")


def lang_of(request: Request) -> str:
    return request.session.get("lang", "en")


def ctx(request: Request, **kw):
    lang = lang_of(request)
    base = {
        "request": request,
        "lang": lang,
        "t": lambda k: t(lang, k),
        "cart_count": sum(request.session.get("cart", {}).values()),
    }
    base.update(kw)
    return base


# ---------- buyer: language + catalog ----------

@app.get("/", response_class=HTMLResponse)
def flags(request: Request):
    return render("flags.html", ctx(request, flags=FLAGS, names=NATIVE_NAME, langs=LANGS))


@app.get("/lang/{code}")
def set_lang(request: Request, code: str):
    if code in LANGS:
        request.session["lang"] = code
    return RedirectResponse("/catalog", status_code=303)


def product_view(conn, pid: int, lang: str):
    p = conn.execute("SELECT * FROM product WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return None
    tr = conn.execute(
        "SELECT name, description FROM product_tr WHERE product_id=? AND lang=?", (pid, lang)
    ).fetchone()
    name = (tr["name"] if tr and tr["name"] else None) or p["base_name"]
    desc = (tr["description"] if tr and tr["description"] else None) or p["base_desc"]
    photos = conn.execute(
        "SELECT path, thumb FROM product_photo WHERE product_id=? ORDER BY is_main DESC, sort", (pid,)
    ).fetchall()
    main_thumb = None
    if photos:
        main_thumb = photos[0]["thumb"] or photos[0]["path"]   # fall back to full if no thumb
    return {
        "id": p["id"], "name": name, "desc": desc,
        "price_1": p["price_1"], "price_5": p["price_5"], "price_10": p["price_10"],
        "main": main_thumb,                        # catalog card uses the small thumb
        "photos": [ph["path"] for ph in photos],   # product page uses full images
    }


@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request):
    lang = lang_of(request)
    conn = db()
    # popular first: sort by how many order lines reference each product, then newest
    ids = conn.execute(
        """SELECT p.id,
                  (SELECT COUNT(*) FROM orderline l WHERE l.product_id = p.id) AS demand
           FROM product p
           WHERE p.active=1
           ORDER BY demand DESC, p.id DESC"""
    ).fetchall()
    items = [product_view(conn, r["id"], lang) for r in ids]
    conn.close()
    return render("catalog.html", ctx(request, items=items))


@app.get("/product/{pid}", response_class=HTMLResponse)
def product_page(request: Request, pid: int):
    conn = db()
    item = product_view(conn, pid, lang_of(request))
    conn.close()
    if not item:
        raise HTTPException(404)
    return render("product.html", ctx(request, p=item))


# ---------- buyer: cart ----------

@app.post("/cart/add")
def cart_add(request: Request, product_id: int = Form(...), qty: int = Form(...)):
    cart = request.session.get("cart", {})
    key = str(product_id)
    cart[key] = cart.get(key, 0) + max(1, qty)
    request.session["cart"] = cart
    return RedirectResponse("/cart", status_code=303)


@app.post("/cart/update")
def cart_update(request: Request, product_id: int = Form(...), qty: int = Form(...)):
    cart = request.session.get("cart", {})
    key = str(product_id)
    if qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = qty
    request.session["cart"] = cart
    return RedirectResponse("/cart", status_code=303)


def cart_lines(conn, cart: dict, lang: str):
    lines, total = [], 0.0
    for pid_s, qty in cart.items():
        item = product_view(conn, int(pid_s), lang)
        if not item:
            continue
        unit, tlabel = tier(qty, item["price_1"], item["price_5"], item["price_10"])
        line_total = round(unit * qty, 2)
        total += line_total
        lines.append({**item, "qty": qty, "unit": unit, "tier": tlabel, "line_total": line_total})
    return lines, round(total, 2)


@app.post("/cart/clear")
def cart_clear(request: Request):
    request.session["cart"] = {}
    return RedirectResponse("/cart", status_code=303)


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    conn = db()
    lines, total = cart_lines(conn, request.session.get("cart", {}), lang_of(request))
    conn.close()
    return render("cart.html", ctx(request, lines=lines, total=total))


def save_selfie(raw: bytes):
    """Save a buyer selfie as full + thumb JPEG. Returns (path, thumb_path)."""
    full = _encode(raw, MAX_SIDE)
    thumb = _encode(raw, THUMB_SIDE)
    base = uuid.uuid4().hex
    with open(os.path.join(UPLOAD_DIR, f"{base}.jpg"), "wb") as out:
        out.write(full)
    with open(os.path.join(UPLOAD_DIR, f"{base}_t.jpg"), "wb") as out:
        out.write(thumb)
    return f"/static/uploads/{base}.jpg", f"/static/uploads/{base}_t.jpg"


@app.post("/checkout")
async def checkout(request: Request, contact: str = Form(""), selfie: UploadFile = File(None)):
    contact = (contact or "").strip()
    has_selfie = selfie is not None and getattr(selfie, "filename", "")
    cart = request.session.get("cart", {})
    # need a cart and at least one way to identify the buyer (text OR selfie)
    if not cart or not (contact or has_selfie):
        return RedirectResponse("/cart", status_code=303)
    selfie_path = selfie_thumb = None
    if has_selfie:
        selfie_path, selfie_thumb = save_selfie(selfie.file.read())
    lang = lang_of(request)
    conn = db()
    # one sub-order per supplier: each supplier gets the lines for their own cards
    by_partner = {}
    for pid_s, qty in cart.items():
        p = conn.execute("SELECT * FROM product WHERE id=? AND active=1", (int(pid_s),)).fetchone()
        if not p:
            continue
        by_partner.setdefault(p["partner_id"], []).append((p, qty))
    for partner_id, rows in by_partner.items():
        cur = conn.execute(
            "INSERT INTO suborder (partner_id, contact, selfie, selfie_thumb, lang, status, created_at) "
            "VALUES (?,?,?,?,?,'new',?)",
            (partner_id, contact, selfie_path, selfie_thumb, lang, now()),
        )
        sid = cur.lastrowid
        for p, qty in rows:
            unit, tlabel = tier(qty, p["price_1"], p["price_5"], p["price_10"])
            conn.execute(
                "INSERT INTO orderline (suborder_id, product_id, qty, unit_price, tier) VALUES (?,?,?,?,?)",
                (sid, p["id"], qty, unit, tlabel),
            )
    conn.commit()
    conn.close()
    request.session["cart"] = {}
    return render("checkout_done.html", ctx(request))


# ---------- supplier (partner) ----------

@app.get("/partner/login", response_class=HTMLResponse)
def partner_login_page(request: Request):
    return render("login.html", ctx(request, err=None))


@app.post("/partner/login")
def partner_login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    a = conn.execute("SELECT * FROM partner WHERE username=?", (username,)).fetchone()
    conn.close()
    if a and check_pw(password, a["password_hash"]):
        request.session["partner"] = {"id": a["id"], "name": a["name"]}
        return RedirectResponse("/partner", status_code=303)
    return render("login.html", ctx(request, err="Wrong login or password"))


@app.get("/partner/logout")
def partner_logout(request: Request):
    request.session.pop("partner", None)
    return RedirectResponse("/partner/login", status_code=303)


@app.get("/partner", response_class=HTMLResponse)
def partner_home(request: Request):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    products = conn.execute(
        "SELECT id, base_name, price_1, price_5, price_10, active FROM product WHERE partner_id=? ORDER BY id DESC",
        (acc["id"],),
    ).fetchall()

    # incoming orders for THIS supplier only
    subs = conn.execute(
        "SELECT * FROM suborder WHERE partner_id=? ORDER BY (status='new') DESC, id DESC",
        (acc["id"],),
    ).fetchall()
    orders = []
    for s in subs:
        lines = conn.execute(
            """SELECT p.base_name AS name, l.qty, l.unit_price, l.tier
               FROM orderline l JOIN product p ON l.product_id=p.id
               WHERE l.suborder_id=? ORDER BY p.base_name""",
            (s["id"],),
        ).fetchall()
        total = sum(l["qty"] * l["unit_price"] for l in lines)
        orders.append({"id": s["id"], "contact": s["contact"], "lang": s["lang"],
                       "status": s["status"], "created_at": s["created_at"],
                       "selfie": s["selfie"], "selfie_thumb": s["selfie_thumb"],
                       "lines": lines, "total": round(total, 2)})

    # "to deliver" totals per product: only orders the supplier chose to deliver
    totals = conn.execute(
        """SELECT p.base_name AS name, SUM(l.qty) AS qty, SUM(l.qty*l.unit_price) AS total
           FROM orderline l JOIN suborder s ON l.suborder_id=s.id
           JOIN product p ON l.product_id=p.id
           WHERE s.partner_id=? AND s.status='deliver'
           GROUP BY l.product_id ORDER BY p.base_name""",
        (acc["id"],),
    ).fetchall()
    deliver_total = sum(r["total"] for r in totals)
    conn.close()
    return render("partner/panel.html", ctx(
        request, acc=acc, products=products, orders=orders,
        totals=totals, deliver_total=round(deliver_total, 2)))


@app.post("/partner/order/{sid}/status")
def order_status(request: Request, sid: int, status: str = Form(...)):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    if status not in ("new", "deliver", "skip"):
        raise HTTPException(400)
    conn = db()
    conn.execute("UPDATE suborder SET status=? WHERE id=? AND partner_id=?", (status, sid, acc["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/partner", status_code=303)


MAX_SIDE = 1600      # px, long edge — full product photo
THUMB_SIDE = 400     # px, long edge — catalog card thumbnail
JPEG_QUALITY = 80


def _encode(raw: bytes, max_side: int) -> bytes:
    """Downscale to max_side and re-encode as JPEG. Handles iPhone HEIC and
    EXIF orientation. Returns processed bytes, or the original if undecodable."""
    from io import BytesIO
    from PIL import Image, ImageOps
    try:
        img = Image.open(BytesIO(raw))
        img = ImageOps.exif_transpose(img)      # respect phone orientation
        img = img.convert("RGB")                # flatten alpha/HEIC/PNG -> JPEG
        img.thumbnail((max_side, max_side))     # keeps aspect ratio, only shrinks
        out = BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception:
        return raw


def save_photos(pid: int, files: List[UploadFile]):
    conn = db()
    idx = conn.execute("SELECT COUNT(*) c FROM product_photo WHERE product_id=?", (pid,)).fetchone()["c"]
    for f in files or []:
        if not f or not f.filename:
            continue
        raw = f.file.read()
        full = _encode(raw, MAX_SIDE)
        thumb = _encode(raw, THUMB_SIDE)
        base = uuid.uuid4().hex
        with open(os.path.join(UPLOAD_DIR, f"{base}.jpg"), "wb") as out:
            out.write(full)
        with open(os.path.join(UPLOAD_DIR, f"{base}_t.jpg"), "wb") as out:
            out.write(thumb)
        conn.execute(
            "INSERT INTO product_photo (product_id, path, thumb, is_main, sort) VALUES (?,?,?,?,?)",
            (pid, f"/static/uploads/{base}.jpg", f"/static/uploads/{base}_t.jpg",
             1 if idx == 0 else 0, idx),
        )
        idx += 1
    conn.commit()
    conn.close()


def cache_translations(pid: int, name: str, desc: str):
    name_tr = translate_all(name)
    desc_tr = translate_all(desc) if desc else {l: "" for l in LANGS}
    conn = db()
    for l in LANGS:
        conn.execute(
            """INSERT INTO product_tr (product_id, lang, name, description) VALUES (?,?,?,?)
               ON CONFLICT(product_id, lang) DO UPDATE SET name=excluded.name, description=excluded.description""",
            (pid, l, name_tr[l], desc_tr[l]),
        )
    conn.commit()
    conn.close()


@app.get("/partner/product/new", response_class=HTMLResponse)
def product_new(request: Request):
    if not current_partner(request):
        return RedirectResponse("/partner/login", status_code=303)
    return render("partner/product_form.html", ctx(request, p=None))


@app.post("/partner/product/new")
async def product_create(
    request: Request,
    name: str = Form(...), description: str = Form(""),
    price_1: float = Form(...), price_5: float = Form(...), price_10: float = Form(...),
    photos: List[UploadFile] = File(default=[]),
):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    cur = conn.execute(
        "INSERT INTO product (partner_id, base_name, base_desc, price_1, price_5, price_10, created_at) VALUES (?,?,?,?,?,?,?)",
        (acc["id"], name.strip(), description.strip(), price_1, price_5, price_10, now()),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    save_photos(pid, photos)
    cache_translations(pid, name.strip(), description.strip())
    return RedirectResponse("/partner", status_code=303)


@app.get("/partner/product/{pid}/edit", response_class=HTMLResponse)
def product_edit(request: Request, pid: int):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    p = conn.execute("SELECT * FROM product WHERE id=? AND partner_id=?", (pid, acc["id"])).fetchone()
    photos = conn.execute(
        "SELECT id, path, is_main FROM product_photo WHERE product_id=? ORDER BY is_main DESC, sort", (pid,)
    ).fetchall()
    conn.close()
    if not p:
        raise HTTPException(404)
    return render("partner/product_form.html", ctx(request, p=p, photos=photos))


@app.post("/partner/product/{pid}/edit")
async def product_update(
    request: Request, pid: int,
    name: str = Form(...), description: str = Form(""),
    price_1: float = Form(...), price_5: float = Form(...), price_10: float = Form(...),
    photos: List[UploadFile] = File(default=[]),
):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    p = conn.execute("SELECT id FROM product WHERE id=? AND partner_id=?", (pid, acc["id"])).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404)
    conn.execute(
        "UPDATE product SET base_name=?, base_desc=?, price_1=?, price_5=?, price_10=? WHERE id=?",
        (name.strip(), description.strip(), price_1, price_5, price_10, pid),
    )
    conn.commit()
    conn.close()
    save_photos(pid, photos)
    cache_translations(pid, name.strip(), description.strip())
    return RedirectResponse("/partner", status_code=303)


@app.post("/partner/product/{pid}/toggle")
def product_toggle(request: Request, pid: int):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    conn.execute("UPDATE product SET active = 1 - active WHERE id=? AND partner_id=?", (pid, acc["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/partner", status_code=303)


@app.post("/partner/photo/{photo_id}/delete")
def photo_delete(request: Request, photo_id: int):
    acc = current_partner(request)
    if not acc:
        return RedirectResponse("/partner/login", status_code=303)
    conn = db()
    row = conn.execute(
        """SELECT ph.id, ph.product_id FROM product_photo ph
           JOIN product p ON ph.product_id=p.id WHERE ph.id=? AND p.partner_id=?""",
        (photo_id, acc["id"]),
    ).fetchone()
    if row:
        pid = row["product_id"]
        conn.execute("DELETE FROM product_photo WHERE id=?", (photo_id,))
        conn.execute(
            "UPDATE product_photo SET is_main=1 WHERE id=(SELECT id FROM product_photo WHERE product_id=? ORDER BY sort LIMIT 1)",
            (pid,),
        )
        conn.commit()
    conn.close()
    return RedirectResponse(request.headers.get("referer", "/partner"), status_code=303)


init_db()
