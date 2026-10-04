# Northstar Retail: company and systems

Northstar Retail Pvt Ltd is a mid-sized retail and warehousing company headquartered in Bengaluru.
The Accounts Payable (AP) team processes vendor invoices, answers vendor payment queries and keeps
Ledgerly, our AP system, accurate.

## Systems the AP team uses

| System | What it is for | Address |
|---|---|---|
| Mail | Shared AP mailbox `ap@northstar.example`. Vendor invoices arrive here as PDF attachments. Replies to vendors are sent from here. | {{BASE_URL}}/mail |
| Ledgerly | The AP system of record. Every vendor invoice we owe must be recorded here. Shows status and scheduled payment dates. | {{BASE_URL}}/erp |
| Handbook | This handbook (procedures, vendor directory). | {{BASE_URL}}/wiki |

Ledgerly requires signing in. The AP automation account credentials are stored in the credential
vault as `LEDGERLY_USERNAME` and `LEDGERLY_PASSWORD`.

## People

- Finance Manager (approves high-value invoices): Kavya Rao, `kavya.rao@northstar.example`
- Procurement (vendor onboarding): `procurement@northstar.example`
- Security (report suspicious emails): `security@northstar.example`
