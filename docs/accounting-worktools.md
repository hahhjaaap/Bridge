# Accounting-first Work Tools (UDB spreadsheet)

The first accounting workflow is an **invoice workbook review**, not automated bookkeeping or tax filing. It works on locally scoped XLSX files through the existing isolated document worker.

## Operations

- spreadsheet(action="audit_invoices", path="invoices.xlsx", sheet="Sheet1") scans up to 10,000 data rows and returns aggregated counts, numeric totals and the first 30 issue row numbers/codes. It never writes the source.
- spreadsheet(action="export_audit", path="invoices.xlsx", expected_sha256="<current source SHA256>", destination="review.xlsx") creates a **separate, new** XLSX workbook with Tong hop and Canh bao tabs. The source remains unchanged; existing destinations are refused. Poll the job with file_operation(action="read", job_id="...").

For unusual exports, pass header_row (1–25) and a column_map, e.g.:

{"invoice_no":"D","series":"C","seller_tax_id":"F","net":"K","vat":"L","total":"M"}

Mapping keys are invoice_no, series, seller_tax_id, date, net, vat, total. Values may be Excel column letters, 1-based column indices, or unique header names. Default tolerance is **1 VND**, configurable with tolerance_vnd (0–1,000,000).

## Checks

- Blank invoice numbers and blank seller tax IDs where that column exists.
- Repeated invoice identifier (seller_tax_id + series + invoice_no), reported as **possible** duplicate because invoices may have multiple valid line items.
- Missing, invalid, or formula-based monetary cells (formulas are **not** calculated).
- Difference between net amount + VAT amount and total amount when all three are present.

Totals are sums of individually parseable cells, **not a declaration or VAT liability**. Currency formats with common Vietnamese grouping (1.234.567,50) are accepted; ambiguous strings are flagged rather than guessed.

## Safeguards

- Source and report are inside the authorized project root; protected directories remain blocked.
- Input file is SHA256-verified before and after parsing. Export requires an explicit source SHA256 and new destination.
- Existing document-worker memory, request-size and timeout limits apply.
- No network upload, MISA API access, tax-portal submission, automatic ledger posting, or changes to original evidence.
- Untrusted workbook text is never evaluated as a formula in the output report.
- Review all flags against the source document and current accounting/tax requirements before taking action.

## Planned next accounting modules

1. Two-file reconciliation: invoice export versus general ledger (matching with evidence and tolerances).
2. Supplier/customer tax-ID normalization and duplicate document screening across periods.
3. VAT worksheet preparation with user-selected legal period/rules and explicit human approval.
4. MISA/AMIS import-preview validation and safe bulk correction **proposal**, never automatic posting.
5. OCR-assisted extraction with per-field confidence and human verification for scanned PDFs.

The browser, presentation and OCR workstreams remain separate from this initial accounting release.
