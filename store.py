"""
MASTER BUTLER — LOCAL DATA STORE (SQLite twin of schema.sql)

schema.sql is the PostgreSQL blueprint for the Render deployment. Until
that server exists, THIS gives the same tables locally (SQLite ships
inside Python — nothing to install), so real records start accumulating
in database form today. Moving to Render later = copy rows, not rebuild.

    python3 store.py sync     # pull shadow bids + reviews into the DB
    python3 store.py report   # quick counts + learning-lane summary

Same design rules as the blueprint: nothing is deleted, auto_send is a
column defaulted OFF, every decision lands in audit_log.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "data" / "masterbutler.db"
SHADOW = BASE / "data" / "shadow_bids"
REVIEW_LOG = BASE / "data" / "review_log.json"

DDL = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT, email TEXT, phone TEXT,
    jobber_client_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers (email);

CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    address TEXT NOT NULL,
    address_normalized TEXT,
    sqft INTEGER, stories TEXT, roof_material TEXT, pitch TEXT,
    data_source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_properties_addr ON properties (address_normalized);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    stamp TEXT UNIQUE,               -- shadow-record stamp (ingest key)
    customer_id INTEGER REFERENCES customers(id),
    property_id INTEGER REFERENCES properties(id),
    source TEXT NOT NULL,            -- 'email' / 'web_form'
    folder TEXT,                     -- INBOX / [Gmail]/Spam
    raw_subject TEXT,
    services_requested TEXT,         -- JSON list
    kind TEXT NOT NULL,
    duplicate_of TEXT,               -- linked, never dropped
    received_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY,
    request_id INTEGER REFERENCES requests(id),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (status IN ('pending_review','in_review','approved','sent',
                          'accepted','declined','expired','on_hold','archived')),
    confidence INTEGER,
    total REAL,
    office_notes TEXT,               -- JSON list (the ⚠ flags)
    auto_send_allowed INTEGER NOT NULL DEFAULT 0,   -- HARD-LOCKED OFF
    reviewed_by TEXT, escalated_to TEXT,
    hold_reason TEXT, hold_until TEXT,
    jobber_quote_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bids_status ON bids (status);

CREATE TABLE IF NOT EXISTS bid_lines (
    id INTEGER PRIMARY KEY,
    bid_id INTEGER REFERENCES bids(id),
    service TEXT NOT NULL,
    system_price REAL,               -- what the engine proposed
    final_price REAL,                -- what the office approved (NULL until)
    invoiced_price REAL,             -- pre-discount truth from the invoice
    honored_discount REAL,
    next_year_price REAL,
    adjust_reason TEXT CHECK (adjust_reason IN (
        'specialty_windows','heavy_tree_coverage','difficult_roof',
        'rate_pricing_update','new_info_photos','tech_adjustment_last_job',
        'last_quote_too_old','underbid_on_review',
        'difficult_customer_premium','other') OR adjust_reason IS NULL),
    adjust_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bid_lines_service ON bid_lines (service);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    bid_id INTEGER REFERENCES bids(id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,                     -- JSON
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ── CLOUD SHELF (Dallon, Jul 22: "move the learning store to postgres")
# The SQLite file itself lives in the cloud `files` table: every
# connect() pulls the cloud copy when it's newer, every write session
# pushes back. On the cron/web containers (ephemeral disk) that makes
# the learning PERSIST; on the Mac (no DB driver) these are no-ops and
# the local file behaves exactly as before.
_STAMP = None                      # cloud updated_at we last synced with


def _cloud_pull():
    global _STAMP
    try:
        import clouddb
        if not clouddb.available():
            return
        cs = clouddb.file_stamp("masterbutler.db")
        if not cs or cs == _STAMP:
            return                 # no cloud copy yet, or already fresh
        blob = clouddb.get_file("masterbutler.db")
        if blob:
            DB.parent.mkdir(exist_ok=True)
            DB.write_bytes(blob)
            _STAMP = cs
    except Exception as e:
        print(f"  (learning store cloud pull skipped: {e})")


def _cloud_push():
    global _STAMP
    try:
        import clouddb
        if not clouddb.available() or not DB.exists():
            return
        clouddb.put_file("masterbutler.db", DB.read_bytes())
        _STAMP = clouddb.file_stamp("masterbutler.db")
    except Exception as e:
        print(f"  ⚠️ learning store cloud push FAILED (session's learning "
              f"is local-only): {e}")


def connect():
    DB.parent.mkdir(exist_ok=True)
    _cloud_pull()                  # freshest copy wins (no-op off-cloud)
    con = sqlite3.connect(DB)
    con.executescript(DDL)
    return con


def _norm_addr(a):
    return re.sub(r"[^a-z0-9]", "", (a or "").lower())[:60]


def _customer_id(con, name, email, phone=None):
    email = (email or "").lower()
    if email:
        row = con.execute("SELECT id FROM customers WHERE email=?",
                          (email,)).fetchone()
        if row:
            return row[0]
    cur = con.execute("INSERT INTO customers (name,email,phone) VALUES (?,?,?)",
                      (name, email, phone))
    return cur.lastrowid


def _property_id(con, customer_id, address, prop_info=None):
    if not address:
        return None
    norm = _norm_addr(address)
    row = con.execute("SELECT id FROM properties WHERE address_normalized=?",
                      (norm,)).fetchone()
    if row:
        return row[0]        # PROPERTY-FIRST: same home matches regardless
                             # of who owns it now (LaRee's rule)
    p = prop_info or {}
    cur = con.execute(
        "INSERT INTO properties (customer_id,address,address_normalized,"
        "sqft,stories,roof_material,pitch,data_source) VALUES (?,?,?,?,?,?,?,?)",
        (customer_id, address, norm, p.get("sqft"), p.get("stories"),
         p.get("roof_material"), p.get("pitch"), p.get("sqft_source")))
    return cur.lastrowid


def sync():
    """Idempotent: pull every shadow record + review decision into the DB.
    Re-running never duplicates (stamp is the key)."""
    con = connect()
    added_req = added_lines = 0

    for pj in sorted(SHADOW.glob("*.json")):
        rec = json.loads(pj.read_text())
        stamp = pj.stem
        if con.execute("SELECT 1 FROM requests WHERE stamp=?",
                       (stamp,)).fetchone():
            continue
        d = rec.get("draft") or {}
        cust = d.get("customer") or {}
        m = re.search(r"<([^>]+)>", rec.get("from", ""))
        cid = _customer_id(con, cust.get("name") or rec.get("from", "?")
                           .split("<")[0].strip(),
                           cust.get("email") or (m.group(1) if m else ""),
                           cust.get("phone"))
        pid = _property_id(con, cid,
                           cust.get("address") or rec.get("address"),
                           d.get("prop_info"))
        source = ("web_form" if "squarespace" in rec.get("from", "").lower()
                  else "email")
        cur = con.execute(
            "INSERT INTO requests (stamp,customer_id,property_id,source,"
            "folder,raw_subject,services_requested,kind,duplicate_of,"
            "received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (stamp, cid, pid, source, rec.get("folder", "INBOX"),
             rec.get("subject"), json.dumps(rec.get("services") or []),
             rec.get("kind", "other"), rec.get("duplicate_of"),
             rec.get("received")))
        added_req += 1
        bid = d.get("bid")
        if bid:
            bcur = con.execute(
                "INSERT INTO bids (request_id,confidence,total,office_notes)"
                " VALUES (?,?,?,?)",
                (cur.lastrowid, bid.get("confidence"), d.get("total"),
                 json.dumps(bid.get("notes") or [])))
            for s in bid.get("services", []):
                con.execute("INSERT INTO bid_lines (bid_id,service,"
                            "system_price) VALUES (?,?,?)",
                            (bcur.lastrowid, s["name"], s["price"]))
                added_lines += 1
            con.execute("INSERT INTO audit_log (bid_id,actor,action,detail)"
                        " VALUES (?,?,?,?)",
                        (bcur.lastrowid, "system", "created",
                         json.dumps({"stamp": stamp})))

    # review decisions → status + audit (idempotent via audit detail key)
    if REVIEW_LOG.exists():
        for r in json.loads(REVIEW_LOG.read_text()):
            key = json.dumps(r, sort_keys=True)
            if con.execute("SELECT 1 FROM audit_log WHERE detail=?",
                           (key,)).fetchone():
                continue
            row = con.execute(
                "SELECT b.id FROM bids b JOIN requests q ON b.request_id=q.id"
                " WHERE q.stamp=?", (r.get("stamp"),)).fetchone()
            bid_id = row[0] if row else None
            status = {"approve": "approved", "adjusted": "approved",
                      "hold": "on_hold", "escalated": "in_review"} \
                .get(r.get("action"))
            if bid_id and status:
                con.execute("UPDATE bids SET status=?, hold_reason=?, "
                            "hold_until=? WHERE id=?",
                            (status, r.get("hold_reason"),
                             r.get("hold_until"), bid_id))
                if r.get("reason"):
                    con.execute("UPDATE bid_lines SET adjust_reason=?, "
                                "adjust_note=? WHERE bid_id=?",
                                (r["reason"], r.get("note"), bid_id))
            con.execute("INSERT INTO audit_log (bid_id,actor,action,detail)"
                        " VALUES (?,?,?,?)",
                        (bid_id, "office", r.get("action", "?"), key))
    con.commit()
    con.close()
    _cloud_push()                  # the session's learning must outlive
    return added_req, added_lines  # this container (Jul 22)


SERVICE_WORDS = {          # fuzzy bridge: our line names ↔ office catalog names
    "gutter": "gutter", "blow": "roof blow", "roof blow": "roof blow",
    "moss": "moss", "driveway": "driveway",
    "patio": "patio", "sidewalk": "sidewalk", "house wash": "house wash",
}


def _window_key(n):
    """Exterior vs in-&-out windows are DIFFERENT services with different
    prices — keep their history in separate buckets so they never
    cross-match (Kimberly Vidos, Jul 10: an exterior quote showed the
    price of her past in-&-out job). Unqualified 'window cleaning' keeps
    the generic key."""
    if ("interior" in n or "in & out" in n or "in and out" in n
            or "in&out" in n or "in/out" in n or "inside" in n):
        return "window_inout"
    if "exterior" in n or "ext only" in n or "outside" in n:
        return "window_exterior"
    return "window"


def _service_key(name):
    n = (name or "").lower()
    if "window" in n:
        return _window_key(n)
    for w, key in SERVICE_WORDS.items():
        if w in n:
            return key
    return None


def record_office_quotes(scoreboard_report):
    """Write the office's real prices into bid_lines.final_price by
    matching scoreboard rows (system vs office) line-by-line. This is
    the learning record: system_price and final_price side by side."""
    con = connect()
    updated = 0
    for row in scoreboard_report.get("rows", []):
        if not row.get("office_quote"):
            continue
        bid = con.execute(
            "SELECT b.id FROM bids b JOIN requests q ON b.request_id=q.id "
            "WHERE q.stamp=?", (row["stamp"],)).fetchone()
        if not bid:
            continue
        con.execute("UPDATE bids SET jobber_quote_id=? WHERE id=?",
                    (str(row["office_quote"]), bid[0]))
        office_by_key = {}
        for li in row.get("office_lines", []):
            k = _service_key(li["name"])
            if k:
                office_by_key[k] = office_by_key.get(k, 0) + (li["price"] or 0)
        for line_id, service in con.execute(
                "SELECT id, service FROM bid_lines WHERE bid_id=? "
                "AND final_price IS NULL", (bid[0],)).fetchall():
            k = _service_key(service)
            if k and k in office_by_key:
                con.execute("UPDATE bid_lines SET final_price=? WHERE id=?",
                            (office_by_key[k], line_id))
                updated += 1
        con.execute("INSERT INTO audit_log (bid_id,actor,action,detail) "
                    "VALUES (?,?,?,?)",
                    (bid[0], "scoreboard", "office_quote_matched",
                     json.dumps({"quote": row["office_quote"],
                                 "office_total": row.get("office_total")})))
    con.commit()
    con.close()
    _cloud_push()                  # ditto (Jul 22)
    return updated


def report():
    con = connect()
    out = {}
    for t in ("customers", "properties", "requests", "bids", "bid_lines",
              "audit_log"):
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    out["by_status"] = dict(con.execute(
        "SELECT status, COUNT(*) FROM bids GROUP BY status").fetchall())
    con.close()
    return out


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "sync":
        r, l = sync()
        print(f"synced: {r} new request(s), {l} bid line(s)")
    print(json.dumps(report(), indent=1))
