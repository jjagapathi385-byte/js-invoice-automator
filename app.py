"""PO -> Zoho invoice automation.
Flow: upload PO -> match quotation -> preview -> confirm -> create invoice linked to the quotation,
update, attach PO, then email (if recipients) or leave as draft.
Zoho endpoints marked VERIFY must be checked against the Zoho Invoice API reference."""
import io, os, re, time, secrets
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for
from pypdf import PdfReader
from rapidfuzz import fuzz

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")
API_URL = os.environ.get("ZOHO_API_URL", "https://www.zohoapis.in/invoice/v3")
ORG_ID = os.environ.get("ZOHO_ORG_ID", "")
APP_PIN = os.environ.get("APP_PIN", "")
SUPPLIER = os.environ.get("SUPPLIER_NAME", "JS Integrated Solutions")

PENDING = {}  # token -> plan, in memory; cleared after commit or restart
STATUS_OPTIONS = [("all", "All"), ("draft", "Draft"), ("sent", "Sent"),
                  ("accepted", "Accepted"), ("invoiced", "Invoiced")]
# GSTIN prefix -> (Zoho place_of_supply letter code, state name)
STATE_CODES = {"36": ("TG", "Telangana"), "37": ("AP", "Andhra Pradesh"), "29": ("KA", "Karnataka"),
               "27": ("MH", "Maharashtra"), "33": ("TN", "Tamil Nadu"), "07": ("DL", "Delhi"),
               "06": ("HR", "Haryana")}
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]{2})\b")
STORE_RE = re.compile(r"\b([KYPZCV]\d{2,4})\b", re.IGNORECASE)
AMOUNT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2})(?!\d)")
PO_RE = re.compile(r"(?:P\.?\s*O\.?\s*#|PO\s*Number|PO\s*No\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{3,})", re.I)


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
    r = requests.post(f"{ACCOUNTS_URL}/oauth/v2/token", data={
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token"}, timeout=20)
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
            p["status"] = status  # VERIFY status values
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
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    return ", ".join(lines)


def extract_addresses(text):
    """Best-effort. Devyani: Bill To block + Delivery Address for ship. Paradise: one block, same for both."""
    bill = ship = ""
    m = re.search(r"Bill\s*To\s*K\d+/(.*?)City\s*:", text, re.S)
    if m:
        bill = _clean(m.group(1))
    m = re.search(r"DeliveryAddress\s*:(.*?)City\s*:", text, re.S)
    if m:
        ship = _clean(m.group(1))
    if not bill:  # Paradise layout: address sits between the S-code line and "Ship LOC GST"
        m = re.search(r"S\d{4}\s*\n(.*?)Ship\s*LOC\s*GST", text, re.S)
        if m:
            bill = _clean(m.group(1))
    if bill and not ship:
        ship = bill  # Paradise: bill-to and ship-to are the same
    return bill, ship


def parse_po(text):
    """Heuristic extraction. Every field is shown in the preview so it can be corrected."""
    po = {"po_number": None, "store": None, "amount": None, "gstin": None,
          "state_code": None, "bill_to": "", "ship_to": "", "items_total": None}
    m = PO_RE.search(text)
    if m:
        po["po_number"] = m.group(1).strip()
    m = STORE_RE.search(text)
    if m:
        po["store"] = m.group(1).upper()
    # Buyer GSTIN: delivery warehouse GSTIN (Devyani) or buyer "Ship LOC GST" (Paradise)
    m = re.search(r"Del\.?\s*Warehouse\s*GSTIN\s*No\s*:?\s*" + GSTIN_RE.pattern, text, re.I)
    if not m:
        m = re.search(r"Ship\s*LOC\s*GST\s*NO\s*:?\s*" + GSTIN_RE.pattern, text, re.I)
    if m:
        po["gstin"] = m.group(m.lastindex)
        po["state_code"] = po["gstin"][:2]
    po["bill_to"], po["ship_to"] = extract_addresses(text)
    m = re.search(r"Total\s*Amount\s*:?\s*([\d,]+\.\d{2})", text, re.I) or \
        re.search(r"Invoice\s*Total\s*\n?\s*([\d,]+\.\d{2})", text, re.I)
    if m:
        po["amount"] = float(m.group(1).replace(",", ""))
    return po


# ---------- matching (same scoring as PO Matcher) ----------
def score(est, store, amount):
    s, reasons = 0, []
    text = f"{est.get('customer_name', '')} {est.get('reference_number', '')}"
    if store and re.search(rf"\b{re.escape(store)}\b", text, re.I):
        s += 50; reasons.append(f"store {store}")
    total, sub = float(est.get("total") or 0), float(est.get("sub_total") or 0)
    if amount:
        if abs(total - amount) <= 1:
            s += 40; reasons.append("total matches")
        elif sub and abs(sub - amount) <= 1:
            s += 35; reasons.append("pre-tax matches")
        elif abs(total - amount) / amount <= 0.02:
            s += 15; reasons.append("total within 2%")
    return s, reasons


# ---------- customer emails: stored in Zoho contact persons ----------
def zoho_contact_emails(customer_id):
    """Return (emails, contact_persons) for a Zoho customer."""
    if not customer_id:
        return [], []
    d = zh("GET", f"/contacts/{customer_id}")  # VERIFY path
    persons = d.get("contact", {}).get("contact_persons", []) or []
    emails = [p["email"] for p in persons if p.get("email")]
    return emails, persons


def add_contact_email(customer_id, email):
    """Add an email to the customer's Zoho contact persons if it isn't there yet."""
    emails, persons = zoho_contact_emails(customer_id)
    if email and email not in emails:
        persons.append({"email": email, "first_name": "", "last_name": email.split("@")[0],
                        "is_primary_contact": False})
        zh("PUT", f"/contacts/{customer_id}", json={"contact_persons": persons})  # VERIFY fields


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
                    float(request.form["amount"].replace(",", "")) if request.form.get("amount") else None)
                if not fields["amount"] and not fields["store"]:
                    error = "Could not find store or amount in this PO."
                else:
                    try:
                        ests = fetch_estimates(status)
                        scored = []
                        for e in ests:
                            s, why = score(e, fields["store"], fields["amount"])
                            if s:
                                scored.append({"id": e.get("estimate_id"), "number": e.get("estimate_number"),
                                               "customer": e.get("customer_name"), "customer_id": e.get("customer_id"),
                                               "date": e.get("date"), "status": e.get("status"),
                                               "total": e.get("total"), "score": s, "reasons": why})
                        results = sorted(scored, key=lambda x: x["score"], reverse=True)[:10]
                        if not results:
                            error = "No matching quotations for this filter."
                    except Exception as exc:
                        error = f"Zoho search failed: {exc}"
    return render_template("index.html", results=results, fields=fields, error=error,
                           status=status, status_options=STATUS_OPTIONS,
                           po_token=po_token)


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

    est = zh("GET", f"/estimates/{est_id}")["estimate"]  # VERIFY path
    if est.get("invoiced") or est.get("status") == "invoiced":
        return render_template("error.html", msg="This quotation is already converted to an invoice.")

    gstin = po.get("gstin") or request.form.get("gstin", "")
    gst_prefix = gstin[:2] if gstin else ""
    state_code, state_name = STATE_CODES.get(gst_prefix, ("", ""))
    local = gst_prefix == "36"

    bill = (request.form.get("bill_to") or po.get("bill_to", "")).strip()
    ship = (request.form.get("ship_to") or po.get("ship_to", "")).strip() or bill
    token = secrets.token_urlsafe(16)
    PENDING[token] = {
        "estimate_id": est_id, "customer_id": customer_id, "po_number": po_number,
        "po_bytes": po_bytes, "po_name": po_name or "PO.pdf", "gstin": gstin,
        "state_code": state_code, "state_name": state_name, "local": local,
        "bill_to": bill, "ship_to": ship, "customer_name": est.get("customer_name", ""),
        "estimate_number": est.get("estimate_number", ""),
        "total": est.get("total"), "created": datetime.now().isoformat(),
    }
    try:
        emails_saved, _ = zoho_contact_emails(customer_id)
    except Exception:
        emails_saved = []
    return render_template("preview.html", p=PENDING[token], token=token,
                           emails_saved=emails_saved, supplier=SUPPLIER)


@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    """Execute: create invoice, update, attach PO, then email or leave as draft."""
    p = PENDING.pop(request.form.get("token", ""), None)
    if not p:
        return render_template("error.html", msg="Preview expired. Start again.")

    try:
        # Recipients
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

        # 1. Fetch quotation and build complete line items (supports both catalog & ad-hoc items)
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

        inv_payload = {
            "customer_id": est["customer_id"],
            "estimate_id": p["estimate_id"],
            "line_items": line_items,
            "payment_terms": 0,
            "payment_terms_label": "Due on Receipt",
            "reference_number": p["po_number"],
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

        # 2. Update addresses on the created invoice
        addr_body = {}
        if p["bill_to"]:
            addr_body["billing_address"] = {"address": p["bill_to"]}
        if p["ship_to"]:
            addr_body["shipping_address"] = {"address": p["ship_to"]}
        if addr_body:
            zh("PUT", f"/invoices/{inv_id}", json=addr_body)

        # 3. Attach PO and mark can_send_in_mail=true so Zoho includes it in the email
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

        # 4. Email (prepending greeting to Zoho's default template) or leave as draft
        if to:
            tpl = zh("GET", f"/invoices/{inv_id}/email")
            default_body = tpl.get("data", {}).get("body", "")
            default_subject = tpl.get("data", {}).get(
                "subject", f"Invoice {inv.get('invoice_number', '')} from {SUPPLIER}"
            )
            greeting = f"<p>Dear {greet_name or p['customer_name']},</p>" if "<" in default_body else f"Dear {greet_name or p['customer_name']},\n\n"
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