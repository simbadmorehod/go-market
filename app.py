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
    """Unit price pulled by the quantity the buyer entered: 1 / 5+ / 10+."""
    if qty >= 10:
        return p10, "10+"
    if qty >= 5:
        return p5, "5+"
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
            is_main INTEGER DEFAULT 0,
            sort INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS suborder (
            id INTEGER PRIMARY KEY,
            partner_id INTEGER NOT NULL REFERENCES partner(id),
            contact TEXT NOT NULL,
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
        "SELECT path FROM product_photo WHERE product_id=? ORDER BY is_main DESC, sort", (pid,)
    ).fetchall()
    return {
        "id": p["id"], "name": name, "desc": desc,
        "price_1": p["price_1"], "price_5": p["price_5"], "price_10": p["price_10"],
        "main": photos[0]["path"] if photos else None,
        "photos": [ph["path"] for ph in photos],
    }


@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request):
    lang = lang_of(request)
    conn = db()
    ids = conn.execute("SELECT id FROM product WHERE active=1 ORDER BY id DESC").fetchall()
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


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    conn = db()
    lines, total = cart_lines(conn, request.session.get("cart", {}), lang_of(request))
    conn.close()
    return render("cart.html", ctx(request, lines=lines, total=total))


@app.post("/checkout")
def checkout(request: Request, contact: str = Form(...)):
    contact = contact.strip()
    cart = request.session.get("cart", {})
    if not contact or not cart:
        return RedirectResponse("/cart", status_code=303)
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
            "INSERT INTO suborder (partner_id, contact, lang, status, created_at) VALUES (?,?,?,'new',?)",
            (partner_id, contact, lang, now()),
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


def save_photos(pid: int, files: List[UploadFile]):
    conn = db()
    idx = conn.execute("SELECT COUNT(*) c FROM product_photo WHERE product_id=?", (pid,)).fetchone()["c"]
    for f in files or []:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
        fname = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
            out.write(f.file.read())
        conn.execute(
            "INSERT INTO product_photo (product_id, path, is_main, sort) VALUES (?,?,?,?)",
            (pid, f"/static/uploads/{fname}", 1 if idx == 0 else 0, idx),
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
