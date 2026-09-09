"""Create supplier accounts and demo products.
Run once:  python seed.py
Change the passwords before real use."""
from app import db, hash_pw, init_db, now

init_db()
conn = db()

def upsert_partner(name, username, password):
    row = conn.execute("SELECT id FROM partner WHERE username=?", (username,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO partner (name, username, password_hash) VALUES (?,?,?)",
        (name, username, hash_pw(password)),
    )
    return cur.lastrowid

# --- suppliers (CHANGE PASSWORDS) ---
colombo = upsert_partner("Colombo Supplier", "colombo", "colombo123")
kandy = upsert_partner("Kandy Supplier", "kandy", "kandy123")

def add_products(partner_id, items):
    has = conn.execute("SELECT COUNT(*) c FROM product WHERE partner_id=?", (partner_id,)).fetchone()["c"]
    if has:
        return
    for name, desc, p1, p5, p10 in items:
        cur = conn.execute(
            "INSERT INTO product (partner_id, base_name, base_desc, price_1, price_5, price_10, created_at) VALUES (?,?,?,?,?,?,?)",
            (partner_id, name, desc, p1, p5, p10, now()),
        )
        pid = cur.lastrowid
        for l in ("zh", "ms", "th", "en"):
            conn.execute(
                "INSERT INTO product_tr (product_id, lang, name, description) VALUES (?,?,?,?)",
                (pid, l, name, desc),
            )

add_products(colombo, [
    ("Rice 5kg bag", "White rice, 5kg", 8.00, 36.00, 65.00),
    ("Cooking oil 1L", "Sunflower oil", 3.50, 15.50, 28.00),
])
add_products(kandy, [
    ("Phone charger", "USB-C fast charger", 6.00, 25.00, 42.00),
    ("Power bank 10000mAh", "Portable battery", 14.00, 62.50, 110.00),
])

conn.commit()
conn.close()
print("Seeded suppliers: colombo/colombo123 , kandy/kandy123")
