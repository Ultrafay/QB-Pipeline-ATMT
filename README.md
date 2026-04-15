# ATH Invoice Pipeline

Automated invoice processing pipeline. Extracts structured data from PDF/image invoices using GPT-4o Vision, applies UAE VAT tax codes, syncs bills to QuickBooks Online, and logs everything to Google Sheets.

Deployed on Railway at `web-production-99a01.up.railway.app`.

## Architecture

```
Google Drive (watched folder)
  |
  v
drive_watcher.py  -->  drive_processor.py (polling worker)
  |
  v
app.py (FastAPI)  -->  ocr_engine.py (orchestrator)
  |
  +-- openai_extractor.py    GPT-4o Vision: PDF/image -> structured JSON
  +-- vat_processor.py       Per-line UAE VAT tax codes (SR/EX/ZR/RC/IG)
  +-- gl_classifier.py       Sheet-driven GL account categorisation
  +-- quickbooks.py          QBO bill sync + RCM journal entries
  +-- sheets_service.py      Google Sheets logging + duplicate detection
```

## Key Features

- **GPT-4o extraction**: Converts invoices (PDF or image) into structured line-item data with supplier info, amounts, and dates.
- **UAE VAT handling**: Assigns tax codes per line item — Standard Rated (SR), Exempt (EX), Zero Rated (ZR), Reverse Charge (RC), and Input on Imports (IG).
- **QuickBooks Online sync**: Creates bills with correct vendor mapping, GL accounts, and tax lines. Posts RCM journal entries automatically for reverse-charge invoices.
- **GL classification**: Maps line items to chart-of-accounts categories using a Google Sheet-driven mapping table.
- **Google Drive polling**: Watches a configured Drive folder for new invoices and processes them automatically.
- **Google Sheets logging**: Appends every processed invoice to a tracking sheet with QBO status, duplicate flags, and file references.
- **Duplicate detection**: Checks invoice number + supplier name against the sheet before processing.
- **OAuth flow**: Built-in `/auth/quickbooks/connect` endpoint for QBO token management via browser.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/launch` | Landing page (post-auth) |
| `POST` | `/api/extract` | Upload and process a single invoice |
| `GET` | `/api/invoices` | List processed invoices from Sheets |
| `POST` | `/api/invoices/{id}/approve` | Mark invoice as approved |
| `POST` | `/api/invoices/{id}/push-to-qb` | Push invoice to QuickBooks |
| `GET` | `/api/drive-watcher/status` | Drive watcher status |
| `GET` | `/auth/quickbooks/connect` | Start QBO OAuth flow |
| `GET` | `/auth/quickbooks/callback` | QBO OAuth callback |
| `POST` | `/auth/quickbooks/disconnect` | Revoke QBO tokens |
| `GET` | `/api/qbo/status` | Check QBO connection status |

## Deployment

The app runs on Railway using the `Dockerfile`. Required environment variables:

- `OPENAI_API_KEY` — GPT-4o API key
- `GOOGLE_SERVICE_ACCOUNT_CONTENT` — Google service account JSON (as string)
- `GOOGLE_SHEET_ID` — Target spreadsheet for invoice logging
- `GOOGLE_DRIVE_FOLDER_ID` — Drive folder to watch for new invoices
- `GL_MAPPING_SHEET_ID` — Spreadsheet with GL mapping rules
- `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET` — QuickBooks OAuth app credentials
- `QBO_REALM_ID`, `QBO_ACCESS_TOKEN`, `QBO_REFRESH_TOKEN` — QBO tokens (set via OAuth flow)
- `QBO_REDIRECT_URI` — OAuth callback URL (defaults to localhost)
- `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_ID`, `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` — For persisting QBO tokens to Railway env vars

## Project Structure

```
app.py                          FastAPI server + QBO OAuth
ocr_engine.py                   Extraction orchestrator
services/
  openai_extractor.py           GPT-4o Vision extraction
  vat_processor.py              UAE VAT per-line tax codes
  gl_classifier.py              Sheet-driven GL categorisation
  gl_reference_data.py          GL keyword mapping reference
  quickbooks.py                 QBO bill sync + RCM journals
  sheets_service.py             Google Sheets logging
  drive_watcher.py              Drive folder watcher
workers/
  drive_processor.py            Background Drive polling worker
utils/
  credentials_helper.py         Google credentials loader
static/
  index.html                    Web dashboard
  auth_success.html             OAuth success page
Dockerfile                      Railway deployment image
requirements.txt                Python dependencies
railway.json                    Railway build config
```
