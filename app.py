"""PO -> Zoho invoice automation.
Flow: upload PO -> match quotation -> preview -> confirm -> create invoice linked to the quotation,
update addresses/fields, attach PO, then email (if recipients) or leave as draft."""
import io
import os
import re
import secrets
import time
from datetime import date, datetime
from functools import wraps

import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://zoho.in")
API_URL = os.environ.get("ZOHO_API_URL", "https://zohoapis.in")
ORG_ID = os.environ.get("ZOHO_ORG_ID", "")
APP_PIN = os.environ.get("APP_PIN", "")
SUPPLIER = os.environ.get("SUPPLIER_NAME", "JS Integrated Solutions")

PENDING = {}
STATUS_OPTIONS = [
    ("all", "All"),
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("accepted", "Accepted"),
    ("invoiced", "Invoiced"),
]

STATE_CODES = {
    "36": ("TS", "Telangana"),
    "37": ("AP", "Andhra Pradesh"),
    "29": ("KA", "Karnataka"),
    "27": ("MH", "Maharashtra"),
    "33": ("TN", "Tamil Nadu"),
    "07": ("DL", "Delhi"),
    "06": ("HR", "Haryana"),
    "19": ("WB", "West Bengal"),
    "24": ("GJ", "Gujarat"),
    "09": ("UP", "Uttar Pradesh"),
    "32": ("KL", "Kerala"),
}
STATE_NAME_TO_CODE = {v[1].upper(): (v[0], v[1]) for v in STATE_CODES.values()}

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]{2})\b")
STORE_RE = re.compile(r"\b([KYPZCVS]\d{3,4})\b", re.IGNORECASE)
PO_RE = re.compile(
    r"(?:P\.?\s*O\.?\s*#|PO\s*Number|PO\s*No\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{3,})",
    re.I,
)


# ---------- access ----------
def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if APP_PIN and not session.get("ok"):
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("pin") == APP_PIN:
            session["ok"] = True
            return redirect(url_for("index"))
        flash("Wrong PIN")
    return render_template("login.html")


# ---------- Zoho ----------
_tok = {"v": None, "exp": 0}


def zoho_token():
    if _tok["v"] and time.time() < _tok["exp"]:
        return _tok["v"]
    r = requests.post(
        f"{ACCOUNTS_URL}/oauth/v2/token",
        data={
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
            "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    _tok.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)) - 120)
    return _tok["v"]


def zh(method, path, **kw):
    headers = {"Authorization": f"Zoho-oauthtoken {zoho_token()}"}
    params = {"organization_id": ORG_ID, **kw.pop("params", {})}
    r = requests.request(method, f"{API_URL}{path}", headers=headers, params=params, timeout=40, **kw)
    r.raise_for_status()
    return r.json()


def fetch_estimates(status):
    out, page = [], 1
    while page <= 10:
        p = {"page": page, "per_page": 200, "sort_column": "date", "sort_order": "D"}
        if status != "all":
            p["status"] = status
        d = zh("GET", "/estimates", params=p)
        out.extend(d.get("estimates", []))
        if not d.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    return out


# ---------- PO reading ----------
def read_pdf_text(f):
    data = f.read() if hasattr(f, "read") else f
    if isinstance(data, bytes):
        f = io.BytesIO(data)
    return "\n".join((p.extract_text() or "") for p in PdfReader(f).pages)


def _clean(block):
    lines = [re.sub(r"\s+", " ", l).strip(" ,") for l in block.splitlines() if l.strip()]
    return ", ".join(lines)


def extract_addresses(text):
    """Extract Bill To and Ship To for Devyani and Paradise PO layouts."""
    bill = ship = ""

    # 1. Devyani layout
    m_bill = re.search(
        r"Bill\s*To[^\n]*\n(.*?)(?=Delivery\s*Warehouse\s*:)", text, re.S | re.I
    )
    if m_bill:
        raw_bill = re.sub(r"^[A-Z]\d+/", "", m_bill.group(1).strip())
        bill = _clean(raw_bill)

    m_ship = re.search(
        r"Delivery\s*Warehouse\s*:\s*(.*?)(?=\n\s*S\.?No\b|\n\s*Material\b)", text, re.S | re.I
    )
    if m_ship:
        raw_ship = m_ship.group(1).replace("DeliveryAddress:", "")
        ship = _clean(raw_ship)

    # 2. Paradise layout (2-column interleaved text)
    if not bill and "Ship/Bill To Address" in text:
        m_para = re.search(
            r"Ship/Bill\s*To\s*Address\s*\n(.*?)(?=Ship\s*LOC\s*GST)", text, re.S | re.I
        )
        if m_para:
            lines = [l.strip() for l in m_para.group(1).splitlines() if l.strip()]
            right_col = []
            for line in lines:
                if line.startswith(("V1", "J S INTEGRATED", "GSTIN:")):
                    continue
                m_s = re.search(r"\b(S\d{3,4})\b", line)
                if m_s:
                    right_col.append(m_s.group(1))
                    continue
                if "Moosapet," in line:
                    right_col.append(line.split("Moosapet,", 1)[1].strip())
                    continue
                if "500018" in line:
                    right_col.append(line.split("500018", 1)[1].strip())
                    continue
                right_col.append(line)
            bill = "Paradise Food Court Private Limited, " + _clean("\n".join(right_col))
            ship = bill

    if bill and not ship:
        ship = bill
    return bill, ship


def parse_po(text):
    """Extract PO Number, Store Code, Total Amount, Buyer GSTIN, State, and Addresses."""
    po = {
        "po_number": None,
        "store": None,
        "amount": None,
        "gstin": None,
        "state_code": None,
        "state_name": "",
        "bill_to": "",
        "ship_to": "",
    }
    m = PO_RE.search(text)
    if m:
        po["po_number"] = m.group(1).strip()

    m = STORE_RE.search(text)
    if m:
        po["store"] = m.group(1).upper()

    m = re.search(r"Del\.?\s*Warehouse\s*GSTIN\s*No\s*:?\s*" + GSTIN_RE.pattern, text, re.I)
    if not m:
        m = re.search(r"Ship\s*LOC\s*GST\s*NO\s*:?\s*" + GSTIN_RE.pattern, text, re.I)
    if m:
        po["gstin"] = m.group(m.lastindex)
        prefix = po["gstin"][:2]
        code, name = STATE_CODES.get(prefix, ("", ""))
        po["state_code"] = code
        po["state_name"] = name

    po["bill_to"], po["ship_to"] = extract_addresses(text)

    if not po["state_code"]:
        m_st = re.search(r"State\s*:\s*([A-Z ]+)", text, re.I)
        if m_st:
            st_key = m_st.group(1).strip().upper()
            if st_key in STATE_NAME_TO_CODE:
                po["state_code"], po["state_name"] = STATE_NAME_TO_CODE[st_key]

    m_inv_tot = re.search(r"Invoice\s*Total\s*(?:[\d,]+\.\d{2}\s+)*([\d,]+\.\d{2})", text, re.I)
    m_tot_amt = re.search(r"Total\s*Amount\s*:?\s*(?:[\d,]+\.\d{2}\s+)*([\d,]+\.\d{2})", text, re.I)
    m_amt = m_inv_tot or m_tot_amt
    if m_amt:
        po["amount"] = float(m_amt.group(1).replace(",", ""))

    return po


# ---------- matching ----------
def score(est, store, amount):
    s, reasons = 0, []
    text = f"{est.get('customer_name', '')} {est.get('reference_number', '')}"
    if store and re.search(rf"\b{re.escape(store)}\b", text, re.I):
        s += 50
        reasons.append(f"store {store}")
    total, sub = float(est.get("total") or 0), float(est.get("sub_total") or 0)
    if amount:
        if abs(total - amount) <= 1:
            s += 40
            reasons.append("total matches")
        elif sub and abs(sub - amount) <= 1:
            s += 35
            reasons.append("pre-tax matches")
        elif abs(total - amount) / amount <= 0.02:
            s += 15
            reasons.append("total within 2%")
    return s, reasons


# ---------- customer emails ----------
def zoho_contact_emails(customer_id):
    if not customer_id:
        return [], []
    d = zh("GET", f"/contacts/{customer_id}")
    persons = d.get("contact", {}).get("contact_persons", []) or []
    emails = [p["email"] for p in persons if p.get("email")]
    return emails, persons


def add_contact_email(customer_id, email):
    """Create a new contact person on the customer without re-sending existing contacts."""
    if not customer_id or not email:
        return
    emails, _ = zoho_contact_emails(customer_id)
    if email.lower() not in [e.lower() for e in emails]:
        zh(
            "POST",
            "/contacts/contactpersons",
            json={
                "contact_id": customer_id,
                "first_name": "",
                "last_name": email.split("@")[0],
                "email": email,
                "is_primary_contact": False,
            },
        )


def build_street1_address(addr_text, state_name=""):
    """Put the full address into Street 1 (address), overflowing into street2 only if > 100 chars."""
    clean = (addr_text or "").strip()
    return {
        "attention": "",
        "address": clean[:100],
        "street2": clean[100:200] if len(clean) > 100 else "",
        "city": "",
        "state": state_name or "",
        "zip": "",
        "country": "India",
        "fax": "",
        "phone": "",
    }


# ---------- routes ----------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    results, fields, error, po_token = [], {}, None, ""
    status = request.values.get("status", "all")
    if status not in dict(STATUS_OPTIONS):
        status = "all"
    if request.method == "POST":
        f = request.files.get("po")
        if not f or not f.filename.lower().endswith(".pdf"):
