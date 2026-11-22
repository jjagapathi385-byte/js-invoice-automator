"""PO -> Zoho invoice automation.
Flow: upload PO -> match quotation (showing PO items & Quote items) -> preview -> confirm ->
sync customer GST/addresses, create invoice with matching IGST/CGST+SGST, attach PO, then email or draft."""
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

ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
API_URL = os.environ.get("ZOHO_API_URL", "https://www.zohoapis.in/invoice/v3")
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
STATE_NAME_TO_CODE = {v[1].upper(): v for v in STATE_CODES.values()}

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


def fetch_estimate_items(est_id):
    """Fetch line items for a matched quotation so they can be compared with PO items."""
    try:
        est = zh("GET", f"/estimates/{est_id}").get("estimate", {})
        items = []
        for li in est.get("line_items", []) or []:
            name = (li.get("name") or li.get("description") or "").strip()
            qty = li.get("quantity", "")
            rate = li.get("rate", "")
            amt = li.get("item_total", "")
            if name:
                items.append({
                    "name": name,
                    "qty": qty,
                    "rate": rate,
                    "amount": amt,
                })
        return items
    except Exception:
        return []


def resolve_tax_id(is_interstate, fallback_tax_id=""):
    """Fetch Zoho Invoice taxes and pick the matching 18% IGST (interstate) or 18% GST (intrastate) tax_id."""
    try:
        taxes = zh("GET", "/settings/taxes").get("taxes", []) or []
        target_pct = 18.0
        igst_matches = []
        gst_matches = []
        for t in taxes:
            pct = float(t.get("tax_percentage") or 0)
            if abs(pct - target_pct) > 0.01:
                continue
            t_type = (t.get("tax_type") or "").lower()
            t_name = (t.get("tax_name") or "").upper()
            t_id = t.get("tax_id")
            if not t_id:
                continue
            if "igst" in t_name or t_type == "tax":
                igst_matches.append(t_id)
            if t_type == "tax_group" or "gst" in t_name and "igst" not in t_name:
                gst_matches.append(t_id)
        if is_interstate and igst_matches:
            return igst_matches[0]
        if not is_interstate and gst_matches:
            return gst_matches[0]
    except Exception:
        pass
    return fallback_tax_id


# ---------- PO reading ----------
def read_pdf_text(f):
    data = f.read() if hasattr(f, "read") else f
    if isinstance(data, bytes):
        f = io.BytesIO(data)
    return "\n".join((p.extract_text() or "") for p in PdfReader(f).pages)


def _clean(block):
    lines = [re.sub(r"\s+", " ", l).strip(" ,") for l in block.splitlines() if l.strip()]
    return ", ".join(lines)


def extract_po_items(text):
    """Extract line items (name, qty, rate, amount) from Paradise, Devyani, and generic PO PDFs."""
    items = []

    # 1. Paradise layout
    if "Ship/Bill To Address" in text or "GRAND TOTAL" in text:
        m_tbl = re.search(r"HSN\s*Code.*?\n(.*?)(?=GRAND\s*TOTAL)", text, re.S | re.I)
        if m_tbl:
            for row in re.finditer(
                r"(?:^|\n)\s*\d+\s+\d{4,6}\s+(.*?)\s+(?:Kilo\s*Gram|Number|Nos?|Set|Lot|Box|Mtr|Kg|Pcs|Unit|Litre|Ltr|Each|Pair)\s+\d{6,8}\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+\d+(?:\.\d+)?\s+[\d,.]+\s+\d+(?:\.\d+)?\s+[\d,.]+\s+([\d,.]+)",
                m_tbl.group(1),
                re.S | re.I,
            ):
                desc = re.sub(r"\s+", " ", row.group(1)).strip()
                items.append({
                    "name": desc,
                    "qty": row.group(2),
                    "rate": row.group(3),
                    "amount": row.group(5),
                })

    # 2. Devyani layout
    if not items and ("Delivery Warehouse:" in text or "Gross Val Inc Tax" in text or "Goods/Services" in text):
        m_tbl = re.search(
            r"(?:Discription|Description|Gross\s*Val\s*Inc\s*Tax|Goods/Services).*?\n(.*?)(?=\n\s*TOTAL\b)",
            text,
            re.S | re.I,
        )
        if m_tbl:
            for line in m_tbl.group(1).splitlines():
                line = line.strip()
                if not line or re.match(r"^(?:of|Goods/Services|Code\s*UNIT|S\.?No|Material)", line, re.I):
                    continue
                m_main = re.match(
                    r"^\d+\s+(?:\d+\s+)?(.*?)\s+(\d+(?:\.\d+)?)\s+[A-Z.]{1,6}\s+([\d,]+\.\d{2})\s+.*?([\d,]+\.\d{2})\s*$",
                    line,
                )
                if m_main:
                    items.append({
                        "name": m_main.group(1).strip(),
                        "qty": m_main.group(2),
                        "rate": m_main.group(3),
                        "amount": m_main.group(4),
                    })
                    continue
                m_sub = re.match(
                    r"^([A-Za-z][A-Za-z0-9\s\-/().,&]+?)\s+(\d+(?:\.\d+)?)\s+[A-Z.]{1,6}\s+([\d,]+\.\d{2})(?:\s+([\d,]+\.\d{2}))?\s*$",
                    line,
                )
                if m_sub:
                    items.append({
                        "name": m_sub.group(1).strip(),
                        "qty": m_sub.group(2),
                        "rate": m_sub.group(3),
                        "amount": m_sub.group(4) or "",
                    })

    # 3. Broad fallback
    if not items:
        for line in text.splitlines():
            line = line.strip()
            m_fb = re.match(
                r"^\d+\s+(?:\d+\s+)?([A-Za-z][A-Za-z0-9\s\-/().,&]{2,60}?)\s+(\d+(?:\.\d+)?)\s+(?:A\.?U|NO|NOS|EA|SET|LOT|KG|MTR|PCS|BOX|UNIT)\s+([\d,]+\.\d{2})(?:.*?([\d,]+\.\d{2}))?\s*$",
                line,
                re.I,
            )
            if m_fb:
                items.append({
                    "name": m_fb.group(1).strip(),
                    "qty": m_fb.group(2),
                    "rate": m_fb.group(3),
                    "amount": m_fb.group(4) or "",
                })

    return items


def extract_addresses(text):
    """Extract Bill To and Ship To for Devyani and Paradise PO layouts."""
    bill = ship = ""

    # 1. Devyani layout
    m_bill = re.search(
        r"Bill\s*To[\W_]*\s*(.*?)(?=Delivery\s*Warehouse\s*:)", text, re.S | re.I
    )
    if m_bill:
        bill = _clean(m_bill.group(1))

    m_ship = re.search(
        r"Delivery\s*Warehouse\s*:\s*(.*?)(?=\n\s*S\.?No\b|\n\s*Material\b)", text, re.S | re.I
    )
    if m_ship:
        raw_ship = m_ship.group(1).replace("DeliveryAddress:", "")
        ship = _clean(raw_ship)

    # 2. Paradise layout
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
    """Extract PO Number, Store Code, Total Amount, Buyer GSTIN, State, Addresses, and Items."""
    po = {
        "po_number": None,
        "store": None,
        "amount": None,
        "gstin": None,
        "state_code": None,
        "state_name": "",
        "bill_to": "",
        "ship_to": "",
        "po_items": [],
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
    po["po_items"] = extract_po_items(text)

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


# ---------- customer emails & profile sync ----------
def zoho_contact_emails(customer_id):
    if not customer_id:
        return [], []
    d = zh("GET", f"/contacts/{customer_id}")
    persons = d.get("contact", {}).get("contact_persons", []) or []
    emails = [p["email"] for p in persons if p.get("email")]
    return emails, persons


def add_contact_email(customer_id, email):
    """Save a new To email to the customer's Zoho contact persons."""
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
            },
        )


def build_address_payload(addr_text, state_name=""):
    """Format Street 1 payload for Zoho's address endpoints."""
    clean = re.sub(r"\s+", " ", (addr_text or "")).strip(" ,")
    return {
        "attention": "",
        "address": clean,
        "street2": "",
        "city": "",
        "state": state_name or "",
        "zip": "",
        "country": "India",
        "fax": "",
        "phone": "",
    }


def sync_customer_details(customer_id, gstin="", state_code="", state_name="", bill_to="", ship_to=""):
    """Update the Zoho customer record directly with new GSTIN, Place of Supply, and Bill/Ship addresses."""
    if not customer_id:
        return
    payload = {}
    if gstin:
        payload["gst_treatment"] = "business_gst"
        payload["gst_no"] = gstin
    if state_code:
        payload["place_of_contact"] = state_code
    if bill_to:
        payload["billing_address"] = build_address_payload(bill_to, state_name)
    if ship_to:
        payload["shipping_address"] = build_address_payload(ship_to, state_name)
    if payload:
        zh("PUT", f"/contacts/{customer_id}", json=payload)


def apply_greeting(default_body, greet_name):
    """Strip any existing 'Dear ...,' line (including HTML tags around it) and replace cleanly with greet_name."""
    if not greet_name:
        return default_body
    # Remove any HTML paragraph/div/span/br line containing 'Dear ...,'
    cleaned = re.sub(
        r"(?:<(?:p|div|span|b|strong)[^>]*>\s*)?Dear\b[^<\r\n]*?(?:,\s*|\s*<br\s*/?>\s*)(?:</(?:p|div|span|b|strong)>\s*)?",
        "",
        default_body,
        count=1,
        flags=re.I,
    ).lstrip()
    if "<" in cleaned:
        return f"<p>Dear {greet_name},</p>\n{cleaned}"
    return f"Dear {greet_name},\n\n{cleaned}"


# ---------- routes ----------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    results, fields, error, po_token = [], {}, None, ""
    status = request.values.get("status", "all")
    if status not in dict(STATUS_OPTIONS):
        status = "all"
    if request.method == "POST":
        try:
            f = request.files.get("po")
            if not f or not f.filename.lower().endswith(".pdf"):
                error = "Upload the PO as a PDF."
            else:
                po_bytes = f.read()
                text = read_pdf_text(po_bytes)
                if not text.strip():
                    error = "No text in this PO (scanned?). Use a text PDF."
                else:
                    fields = parse_po(text)
                    po_token = secrets.token_urlsafe(16)
                    PENDING[po_token] = {"po_bytes": po_bytes, "po_name": f.filename, "fields": fields}
                    fields["store"] = (request.form.get("store") or "").strip() or fields["store"]
                    raw_amt = (request.form.get("amount") or "").strip()
                    if not fields["amount"] and raw_amt:
                        fields["amount"] = float(raw_amt.replace(",", ""))
                    if not fields["amount"] and not fields["store"]:
                        error = "Could not find store or amount in this PO."
                    else:
                        ests = fetch_estimates(status)
                        scored = []
                        for e in ests:
                            s, why = score(e, fields["store"], fields["amount"])
                            if s:
                                scored.append(
                                    {
                                        "id": e.get("estimate_id"),
                                        "number": e.get("estimate_number"),
                                        "customer": e.get("customer_name"),
                                        "customer_id": e.get("customer_id"),
                                        "date": e.get("date"),
                                        "status": e.get("status"),
                                        "total": e.get("total"),
                                        "score": s,
                                        "reasons": why,
                                    }
                                )
                        results = sorted(scored, key=lambda x: x["score"], reverse=True)[:10]
                        for r_item in results:
                            r_item["quote_items"] = fetch_estimate_items(r_item["id"])
                        if not results:
                            error = "No matching quotations for this filter."
        except Exception as exc:
            error = f"Error processing PO: {exc}"
    return render_template(
        "index.html",
        results=results,
        fields=fields,
        error=error,
        status=status,
        status_options=STATUS_OPTIONS,
        po_token=po_token,
    )


@app.route("/preview", methods=["POST"])
@login_required
def preview():
    """Build the full plan. Nothing is written to Zoho here."""
    est_id = request.form["estimate_id"]
    customer_id = request.form.get("customer_id", "")
    po_number = request.form.get("po_number", "").strip()
    stored = PENDING.get(request.form.get("po_token", ""), {})
    po_bytes = stored.get("po_bytes", b"")
    po_name = stored.get("po_name", "")
    po = stored.get("fields", {})

    est = zh("GET", f"/estimates/{est_id}")["estimate"]
    if est.get("invoiced") or est.get("status") == "invoiced":
        return render_template("error.html", msg="This quotation is already converted to an invoice.")

    gstin = (po.get("gstin") or request.form.get("gstin", "")).strip()
    gst_prefix = gstin[:2] if gstin else ""
    state_code, state_name = STATE_CODES.get(
        gst_prefix, (po.get("state_code", ""), po.get("state_name", ""))
    )
    local = gst_prefix == "36"

    bill = (request.form.get("bill_to") or po.get("bill_to", "")).strip()
    ship = (request.form.get("ship_to") or po.get("ship_to", "")).strip() or bill
    token = secrets.token_urlsafe(16)
    PENDING[token] = {
        "estimate_id": est_id,
        "customer_id": customer_id,
        "po_number": po_number,
        "po_bytes": po_bytes,
        "po_name": po_name or "PO.pdf",
        "gstin": gstin,
        "state_code": state_code,
        "state_name": state_name,
        "local": local,
        "bill_to": bill,
        "ship_to": ship,
        "customer_name": est.get("customer_name", ""),
        "estimate_number": est.get("estimate_number", ""),
        "total": est.get("total"),
        "created": datetime.now().isoformat(),
    }
    try:
        emails_saved, _ = zoho_contact_emails(customer_id)
    except Exception:
        emails_saved = []
    return render_template(
        "preview.html", p=PENDING[token], token=token, emails_saved=emails_saved, supplier=SUPPLIER
    )


@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    """Execute: sync customer details, create invoice with IGST/GST, attach PO, then email or leave as draft."""
    p = PENDING.pop(request.form.get("token", ""), None)
    if not p:
        return render_template("error.html", msg="Preview expired. Start again.")

    try:
        to = [e.strip() for raw in request.form.getlist("to") for e in re.split(r"[,;]+", raw) if e.strip()]
        new_to = request.form.get("new_to", "").strip()
        if new_to:
            for e in re.split(r"[,;]+", new_to):
                e = e.strip()
                if e and e not in to:
                    to.append(e)
                    add_contact_email(p["customer_id"], e)

        cc_raw_list = (
            request.form.getlist("cc")
            + request.form.getlist("cc_emails")
            + request.form.getlist("cc_mail_ids")
            + [request.form.get("new_cc", "")]
        )
        cc = []
        for raw in cc_raw_list:
            for e in re.split(r"[,;]+", raw or ""):
                e = e.strip()
                if e and e not in cc:
                    cc.append(e)

        greet_name = request.form.get("greet_name", "").strip()
        p["bill_to"] = request.form.get("bill_to", p["bill_to"]).strip()
        p["ship_to"] = request.form.get("ship_to", p["ship_to"]).strip()
        if request.form.get("gstin"):
            p["gstin"] = request.form.get("gstin", "").strip()
            gst_prefix = p["gstin"][:2]
            if gst_prefix in STATE_CODES:
                p["state_code"], p["state_name"] = STATE_CODES[gst_prefix]
        if request.form.get("state_code"):
            p["state_code"] = request.form.get("state_code", "").strip().upper()
            for code, name in STATE_CODES.values():
                if code == p["state_code"]:
                    p["state_name"] = name
                    break

        cust_id = p.get("customer_id", "")

        # 1. Update Zoho Customer record directly with new GSTIN, Place of Supply, and Bill/Ship addresses
        sync_customer_details(
            cust_id,
            gstin=p.get("gstin", ""),
            state_code=p.get("state_code", ""),
            state_name=p.get("state_name", ""),
            bill_to=p["bill_to"],
            ship_to=p["ship_to"],
        )

        # 2. Fetch quotation and switch tax_id to IGST (interstate) vs CGST+SGST (Telangana / TS)
        est = zh("GET", f"/estimates/{p['estimate_id']}")["estimate"]
        is_interstate = bool(p.get("state_code") and p["state_code"] != "TS")
        resolved_tax = resolve_tax_id(is_interstate)

        line_items = []
        for li in est.get("line_items", []):
            item = {
                "name": li.get("name", ""),
                "description": li.get("description", ""),
                "rate": li["rate"],
                "quantity": li["quantity"],
            }
            if li.get("item_id"):
                item["item_id"] = li["item_id"]
            if resolved_tax:
                item["tax_id"] = resolved_tax
            elif li.get("tax_id"):
                item["tax_id"] = li["tax_id"]
            if li.get("hsn_or_sac"):
                item["hsn_or_sac"] = li["hsn_or_sac"]
            line_items.append(item)

        today_str = date.today().isoformat()
        inv_payload = {
            "customer_id": est["customer_id"],
            "estimate_id": p["estimate_id"],
            "date": today_str,
            "due_date": today_str,
            "payment_terms": 0,
            "payment_terms_label": "Due on Receipt",
            "reference_number": p["po_number"],
            "line_items": line_items,
            "is_inclusive_tax": est.get("is_inclusive_tax", False),
        }
        if p.get("gstin"):
            inv_payload["gst_treatment"] = "business_gst"
            inv_payload["gst_no"] = p["gstin"]
        if p.get("state_code"):
            inv_payload["place_of_supply"] = p["state_code"]

        created = zh("POST", "/invoices", json=inv_payload)
        inv = created["invoice"]
        inv_id = inv["invoice_id"]

        # 3. Also set invoice-level Street 1 billing & shipping addresses
        if p["bill_to"]:
            zh(
                "PUT",
                f"/invoices/{inv_id}/address/billing",
                json=build_address_payload(p["bill_to"], p.get("state_name", "")),
            )
        if p["ship_to"]:
            zh(
                "PUT",
                f"/invoices/{inv_id}/address/shipping",
                json=build_address_payload(p["ship_to"], p.get("state_name", "")),
            )

        # 4. Attach PO with can_send_in_mail=true
        if p["po_bytes"]:
            att_r = requests.post(
                f"{API_URL}/invoices/{inv_id}/attachment",
                headers={"Authorization": f"Zoho-oauthtoken {zoho_token()}"},
                params={"organization_id": ORG_ID},
                data={"can_send_in_mail": "true"},
                files={"attachment": (p["po_name"], p["po_bytes"], "application/pdf")},
                timeout=60,
            )
            att_r.raise_for_status()

        # 5. Send email (with single clean greeting) or save as draft
        if to:
            tpl = zh("GET", f"/invoices/{inv_id}/email")
            default_body = tpl.get("data", {}).get("body", "")
            default_subject = tpl.get("data", {}).get(
                "subject", f"Invoice {inv.get('invoice_number', '')} from {SUPPLIER}"
            )
            final_body = apply_greeting(default_body, greet_name)
            mail = {
                "to_mail_ids": to,
                "subject": default_subject,
                "body": final_body,
                "attach_pdf": True,
            }
            if cc:
                mail["cc_mail_ids"] = cc
            mail_r = requests.post(
                f"{API_URL}/invoices/{inv_id}/email",
                headers={"Authorization": f"Zoho-oauthtoken {zoho_token()}"},
                params={"organization_id": ORG_ID},
                json=mail,
                timeout=60,
            )
            mail_r.raise_for_status()
            cc_note = f" (CC: {', '.join(cc)})" if cc else ""
            result = f"Invoice created and emailed to {', '.join(to)}{cc_note}"
        else:
            result = "Invoice saved as draft (no recipients added)."

        return render_template(
            "done.html",
            result=result,
            inv_number=inv.get("invoice_number", ""),
            inv_id=inv_id,
        )
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return render_template("error.html", msg=f"Zoho API Error: {detail}")
    except Exception as exc:
        return render_template("error.html", msg=f"Unexpected error: {exc}")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
