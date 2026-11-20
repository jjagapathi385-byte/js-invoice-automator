# PO Invoice Automation

Flow: upload PO → matching quotations → review preview → confirm → Zoho invoice
created with PO attached → emailed if recipients were chosen, otherwise saved as draft.

## Setup
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in. Use a Server-based Zoho client with
   create/update scopes, separate from the downloader and QuoteBot.
3. `python app.py`, open http://localhost:5000

## Before first live use (VERIFY items in app.py)
- `GET /estimates` status values: draft, sent, accepted, invoiced.
- `POST /estimates/{id}/invoice` (convert). Confirm the path in the API reference.
- `PUT /invoices/{id}` field names: reference_number, billing_address, shipping_address,
  payment_terms, place_of_supply format, gst_treatment, gst_no.
- `POST /invoices/{id}/attachment` upload field name.
- `POST /invoices/{id}/email` body fields.
- Test with one real quotation on a copy/test customer before using on live invoices.

## Known limits
- Address extraction is not yet automatic. Bill To and Ship To are typed or corrected in the preview.
- Saved emails are kept in customer_emails.json on the server. They are not yet synced to
  the Zoho customer record. That sync is the next step to add.
- Pending previews are held in memory and are lost on restart.
- Scanned PDFs are not read.
