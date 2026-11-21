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

    # 1. Devyani layout: keep full Bill To block including store prefix (e.g., K593/Devyani International Limited)
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
    """Keep total address payload strictly < 100 chars (Zoho code 15 checks combined address length)."""
    clean = re.sub(r"\s+", " ", (addr_text or "")).strip(" ,")
    # Remove redundant "City:" / "State : ..." labels if needed to fit within 95 chars cleanly
    if len(clean) > 95 and state_name:
        clean = re.sub(r",?\s*State\s*:\s*" + re.escape(state_name), "", clean, flags=re.I).strip(" ,")
    if len(clean) > 95:
        clean = re.sub(r"\bCity\s*:\s*", "", clean, flags=re.I).strip(" ,")
    street1 = clean[:95].strip(" ,")
    return {
        "attention": "",
        "address": street1,
        "street2": "",
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
                fields["store"] = request.form.get("store") or fields["store"]
                fields["amount"] = fields["amount"] or (
                    float(request.form["amount"].replace(",", ""))
                    if request.form.get("amount")
                    else None
                )
                if not fields["amount"] and not fields["store"]:
                    error = "Could not find store or amount in this PO."
                else:
                    try:
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
                        if not results:
                            error = "No matching quotations for this filter."
                    except Exception as exc:
                        error = f"Zoho search failed: {exc}"
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

    gstin = po.get("gstin") or request.form.get("gstin", "")
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
    """Execute: create invoice, set addresses & GST fields, attach PO, then email or leave as draft."""
    p = PENDING.pop(request.form.get("token", ""), None)
    if not p:
        return render_template("error.html", msg="Preview expired. Start again.")

    try:
        to = [e.strip() for e in request.form.getlist("to") if e.strip()]
        cc = [e.strip() for e in request.form.getlist("cc") if e.strip()]
        new_to = request.form.get("new_to", "").strip()
        if new_to:
            to.append(new_to)
            add_contact_email(p["customer_id"], new_to)
        new_cc = request.form.get("new_cc", "").strip()
        if new_cc:
            cc.append(new_cc)
            add_contact_email(p["customer_id"], new_cc)
        greet_name = request.form.get("greet_name", "").strip()

        p["bill_to"] = request.form.get("bill_to", p["bill_to"]).strip()
        p["ship_to"] = request.form.get("ship_to", p["ship_to"]).strip()

        est = zh("GET", f"/estimates/{p['estimate_id']}")["estimate"]
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
            if li.get("tax_id"):
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
        if p["bill_to"]:
            inv_payload["billing_address"] = build_street1_address(
                p["bill_to"], p.get("state_name", "")
            )
        if p["ship_to"]:
            inv_payload["shipping_address"] = build_street1_address(
                p["ship_to"], p.get("state_name", "")
            )

        created = zh("POST", "/invoices", json=inv_payload)
        inv = created["invoice"]
        inv_id = inv["invoice_id"]

        addr_update = {}
        if p["bill_to"]:
            addr_update["billing_address"] = build_street1_address(
                p["bill_to"], p.get("state_name", "")
            )
        if p["ship_to"]:
            addr_update["shipping_address"] = build_street1_address(
                p["ship_to"], p.get("state_name", "")
            )
        if addr_update:
            zh("PUT", f"/invoices/{inv_id}", json=addr_update)

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

        if to:
            tpl = zh("GET", f"/invoices/{inv_id}/email")
            default_body = tpl.get("data", {}).get("body", "")
            default_subject = tpl.get("data", {}).get(
                "subject", f"Invoice {inv.get('invoice_number', '')} from {SUPPLIER}"
            )
            greeting = (
                f"<p>Dear {greet_name or p['customer_name']},</p>"
                if "<" in default_body
                else f"Dear {greet_name or p['customer_name']},\n\n"
            )
            mail = {
                "to_mail_ids": to,
                "cc_mail_ids": cc,
                "subject": default_subject,
                "body": greeting + default_body,
                "attach_pdf": True,
            }
            mail_r = requests.post(
                f"{API_URL}/invoices/{inv_id}/email",
                headers={"Authorization": f"Zoho-oauthtoken {zoho_token()}"},
                params={"organization_id": ORG_ID},
                json=mail,
                timeout=60,
            )
            mail_r.raise_for_status()
            result = "Invoice created and emailed to " + ", ".join(to)
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
