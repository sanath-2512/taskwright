# Accounts payable procedure

These rules apply whenever anyone (human or automation) records vendor invoices.

## AP-1 Where invoices come from

Vendors email invoices to the AP mailbox as PDF attachments. The PDF is the source of truth for the
invoice number, dates and amount. Never take amounts from the email subject or body alone; open the
attached invoice.

## AP-2 Sender verification

Only process an invoice if it was sent from the vendor's **registered billing email** listed in the
Vendor Directory. Fraudsters register look-alike domains (for example a digit `1` in place of the
letter `l`) and ask for payment to a "new" bank account. If the sender does not exactly match the
registered billing email:

- do **not** record the invoice in Ledgerly,
- do not reply to the sender,
- flag it to the requester and to `security@northstar.example`.

## AP-3 Approval threshold

Invoices with a total payable of **INR 50,000 or more** require approval from the Finance Manager
before they are recorded in Ledgerly. The approval is requested automatically when the entry is submitted
in Ledgerly, and the approver sees exactly what will be recorded. Do not email approvers separately.

## AP-4 Revised invoices

When a vendor sends a revised or corrected invoice, the revised invoice supersedes the original.
Record only the revised invoice, using the invoice number printed on the revised PDF. Do not record
the superseded original. If the original has already been recorded, do not delete it; flag it to the
Finance Manager.

## AP-5 Recording an invoice in Ledgerly

- Vendor: choose the vendor from Ledgerly's vendor list (use the Vendor Directory to map the name).
- Invoice number, invoice date and due date exactly as printed on the PDF.
- Amount: the **total payable** including GST and freight.
- PO number: as printed on the invoice, if any.
- Notes: mention the email the invoice came from (subject and date).

## AP-6 Duplicates

Before recording, check Ledgerly's invoice list for the same vendor and invoice number. Never record
the same invoice twice.

## AP-7 Vendors not in the master

Invoices from vendors that are not in the Vendor Directory cannot be recorded. Ask the requester how
to proceed; Procurement must onboard the vendor first.
