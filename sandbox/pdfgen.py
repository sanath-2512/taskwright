"""Renders realistic-looking GST tax-invoice PDFs for the sandbox mailbox."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .seed import COMPANY, invoice_totals


def _inr(x: float) -> str:
    # Indian digit grouping: 1,23,456.78
    neg = x < 0
    x = abs(x)
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def render_invoice(inv: dict, path: Path) -> None:
    v = inv["vendor"]
    t = invoice_totals(inv)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Tax Invoice {inv['number']}")
    c.setAuthor(v["name"])
    w, h = A4
    x0, y = 18 * mm, h - 20 * mm

    c.setFont("Helvetica-Bold", 15)
    c.drawString(x0, y, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(x0, y - 13, v["address"])
    c.drawString(x0, y - 25, f"GSTIN: {v['gstin']}   Email: {v['email']}")
    c.setFont("Helvetica-Bold", 17)
    c.drawRightString(w - x0, y, "TAX INVOICE")

    y -= 50
    c.setStrokeColor(colors.HexColor("#999999"))
    c.line(x0, y, w - x0, y)
    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0, y, "Bill to")
    c.setFont("Helvetica", 9)
    c.drawString(x0, y - 12, COMPANY["name"])
    c.drawString(x0, y - 24, COMPANY["address"])
    c.drawString(x0, y - 36, f"GSTIN: {COMPANY['gstin']}")

    meta = [
        ("Invoice No.", inv["number"]),
        ("Invoice Date", inv["date"]),
        ("Due Date", inv["due"]),
        ("PO Number", inv["po"] or "-"),
    ]
    mx = w - x0 - 70 * mm
    for i, (k, val) in enumerate(meta):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(mx, y - i * 12, k)
        c.setFont("Helvetica", 9)
        c.drawString(mx + 28 * mm, y - i * 12, val)

    y -= 62
    if inv.get("note"):
        c.setFillColor(colors.HexColor("#b00020"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x0, y, inv["note"])
        c.setFillColor(colors.black)
        y -= 18

    # line items
    cols = [x0, x0 + 100 * mm, x0 + 118 * mm, x0 + 145 * mm]
    c.setFillColor(colors.HexColor("#eeeeee"))
    c.rect(x0, y - 4, w - 2 * x0, 16, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    for col, head in zip(cols, ["Description", "Qty", "Rate (INR)", "Amount (INR)"]):
        c.drawString(col + 2, y, head)
    y -= 18
    c.setFont("Helvetica", 9)
    for desc, qty, rate in inv["items"]:
        c.drawString(cols[0] + 2, y, desc)
        c.drawString(cols[1] + 2, y, str(qty))
        c.drawString(cols[2] + 2, y, _inr(rate))
        c.drawString(cols[3] + 2, y, _inr(qty * rate))
        y -= 14

    y -= 8
    c.line(cols[2], y + 6, w - x0, y + 6)
    rows = [("Subtotal", t["subtotal"])]
    if t["cgst"]:
        rows += [("CGST @ 9%", t["cgst"]), ("SGST @ 9%", t["sgst"])]
    if t["igst"]:
        rows += [("IGST @ 18%", t["igst"])]
    if t["freight"]:
        rows += [("Freight", t["freight"])]
    for label, val in rows:
        c.drawString(cols[2] + 2, y - 4, label)
        c.drawRightString(w - x0 - 2, y - 4, _inr(val))
        y -= 14
    c.setFont("Helvetica-Bold", 11)
    c.drawString(cols[2] + 2, y - 8, "Total Payable")
    c.drawRightString(w - x0 - 2, y - 8, f"INR {_inr(t['total'])}")

    y -= 50
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0, y, "Payment details")
    c.setFont("Helvetica", 9)
    c.drawString(x0, y - 12, inv["bank"])
    c.drawString(x0, y - 24, "Please quote the invoice number with your remittance.")
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(x0, 20 * mm, "This is a computer generated invoice.")
    c.showPage()
    c.save()
