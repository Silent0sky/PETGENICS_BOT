# Petgenics Telegram Order Bot

Python 3.11+ order intake for Petgenics, backed by PostgreSQL. The Telegram bot is optional during development: without `BOT_TOKEN`, the API starts without a Telegram webhook. No token is included in this repository.

## What is included

- FastAPI service, Telegram webhook and PostgreSQL/SQLAlchemy models.
- Persistent draft orders, order audit events, approved salesperson accounts, and per-person party/brand assignments.
- Admin-protected bulk product and party list updates from the dashboard, Excel preview and column mapping, generated templates, full exports, import logs and downloadable row-error workbooks.
- Column mapping for `.xlsx` and `.xls`; auto-detected header row can be overridden. Numeric product codes formatted with leading zeroes are preserved, duplicate codes in an upload are reported, blank cells do not erase existing product data, and unmapped nonblank source columns are retained.
- Decimal order totals, discount/free-quantity authorization, order review/status decisions and date-wise reports.
- Digital-PDF text extraction, OCR for scanned PDF pages and images, explicit scanner review, and a configurable PostgreSQL file-retention period.
- Admin-configured MARG column mapping. No MARG import headers or rules are assumed.
- Telegram admin panel with searchable/paginated orders, salesperson and catalogue management, reports, imports, MARG/scanner controls, and a persistent command/reply-keyboard menu. Access is restricted to numeric IDs in `ADMIN_TELEGRAM_IDS`.
- Docker/Render configuration and a PostgreSQL schema baseline.

The five supplied price lists are intentionally **not** pre-imported: their columns differ, some have no brand column, and importing them without an administrator mapping/brand choice would guess product information. Preview each workbook and explicitly map its columns and brand before importing it.

## Run locally

1. Use Python 3.11 or later and a PostgreSQL database.
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set `DATABASE_URL`, `ADMIN_API_KEY`, and `HEALTH_CHECK_TOKEN`. Never commit `.env`.
4. Start the API: `uvicorn main:app --reload`.
5. Open `/docs`. Admin operations require the `X-Admin-Key` header. The bot is not initialized unless `BOT_TOKEN` is configured.

The container applies the Alembic migration before starting the API. For local setup, run `alembic upgrade head` once after configuring `DATABASE_URL`. `migrations/001_initial.sql` is also provided as a plain-SQL initial schema for manual provisioning. Do not apply the initial baseline over an existing populated database. Future schema changes should be added as new Alembic revisions.

## Free Render + Neon test deployment

For a no-cost test setup, use a Render Free Docker web service with a Neon Free PostgreSQL database. The app's `render.yaml` defines a Free web service and does not create a paid Render database; provide Neon’s connection string as `DATABASE_URL`. Render Free services can sleep when idle, and Neon Free compute scales to zero. This setup is for testing, not production uptime or guaranteed data recovery. Review the current [Render Free limitations](https://render.com/docs/free) and [Neon plan limits](https://neon.com/pricing).

1. Keep the GitHub repository free of `.env`, tokens, passwords, customer exports, and Excel workbooks.
2. Create a Neon project. Choose the Singapore region if available for your Render service.
3. In Neon, open **Connect**, select your database/user, enable connection pooling, and copy its PostgreSQL connection string. Keep it private.
4. In Render, create a **Blueprint** from this repository if the Blueprint option is available. The included `render.yaml` defines a Free Docker web service. Do not create a paid Render Postgres database for this setup. Alternatively, create a Docker **Web Service** on the Free plan manually.
5. Set `DATABASE_URL` to the Neon connection string, plus generated `ADMIN_API_KEY` and `HEALTH_CHECK_TOKEN`. Add `WEBHOOK_SECRET` using Render's **Generate** option. Initially leave `BOT_TOKEN` unset so the app can deploy before its public URL is known.
6. Deploy and wait for the service to become Live. Copy the exact Render HTTPS service URL; confirm `/healthz` returns `{"status":"ok"}`.
7. Set `WEBHOOK_URL` to that exact HTTPS URL and add `BOT_TOKEN` from BotFather. Use a newly generated token; revoke any token previously shared in chat. Save the settings and wait for Render to redeploy.
8. After deploying the Telegram admin update, send `/myid`, add that numeric ID to Render's `ADMIN_TELEGRAM_IDS`, wait for restart, then use `/admin` to import Excel files, approve salespeople/orders, and receive order exports in Telegram.

The app normalizes Neon’s `postgresql://` URL for SQLAlchemy/asyncpg and converts `sslmode=require` into the asyncpg `ssl=require` option.

Render Free web services sleep after 15 minutes without traffic and can take about a minute to wake. Neon Free compute also suspends after inactivity. Free plans have usage and reliability limits; check provider dashboards regularly. For production, use paid hosting/database plans and a verified backup/recovery process. Do not expose secrets in URLs, source files, screenshots, or chat messages.

The Telegram secret is read from the environment, used as the webhook path component and checked again against Telegram's webhook secret header. Do not paste bot tokens into source files, commits, tickets, or logs. Rotate a token if it is accidentally disclosed.

### UptimeRobot monitoring

The public `https://YOUR-RENDER-SERVICE.onrender.com/healthz` endpoint is ready for UptimeRobot and is also Render's configured health-check path. Create an **HTTP(s)** monitor for that full URL; it returns HTTP 200 with `{"status":"ok"}`. Do not monitor `/health` because that endpoint requires a secret header. UptimeRobot reports endpoint availability but does not restart Render or verify Telegram ordering; free Render services may sleep and take time to wake.

### Telegram admin menu

Set `ADMIN_TELEGRAM_IDS` to one or more comma-separated numeric Telegram user IDs. Each administrator can send `/admin` or tap **Admin panel** to review orders, manage salespeople/catalogue, import workbooks, download date-wise reports, and access scanner/MARG settings. Salespeople and administrators receive a persistent reply keyboard and Telegram command menu; admin-only commands are scoped to configured admin chats. `/adminreport YYYY-MM-DD` downloads a date-wise order workbook. `/adminpermission ID yes/no MAX_DISCOUNT yes/no` sets salesperson discount and free-quantity permissions. Use `/myid` to find your Telegram user ID. `REPORT_UTC_OFFSET_MINUTES` sets the reporting timezone offset from UTC; the default `330` is India Standard Time. MARG exports remain gated on configuring the actual MARG template mapping first; no MARG column names are assumed.

### PostgreSQL backups and recovery

If using a paid Render Postgres database, use Render's backup/PITR facilities where available. For the free Render + Neon test setup, Neon Free has limited recovery features; export important data and keep a separate protected backup. For an additional portable PostgreSQL backup, run from a trusted machine with PostgreSQL client tools and a protected database URL:

```powershell
$env:PGPASSWORD = "your-database-password"
pg_dump "$env:DATABASE_URL" --format=custom --file="petgenics-backup.dump"
pg_restore --list "petgenics-backup.dump"
```

Restore into a newly provisioned database after reviewing the target:

```powershell
pg_restore --clean --if-exists --dbname="$env:DATABASE_URL" "petgenics-backup.dump"
```

Protect and encrypt backup files, restrict access, and delete them under your retention policy. Product masters, orders, audit history, scanner review metadata, and scanner documents within their configured retention period live in PostgreSQL, not solely on Render's ephemeral filesystem.

## Admin API workflows

The `/admin` page is a small dashboard for orders, product imports, templates and MARG mappings. Its data-changing/API requests require `X-Admin-Key`; the key is kept in the current browser tab's session storage. Other admin routes can be used from `/docs`, and still enforce authorization.

### Product import

1. `POST /admin/imports/preview` with an Excel file and worksheet index. The response includes detected headers, sample rows and the detected 1-based header row.
2. Map the exact returned column names to `product_code`, `name`, `brand`, `family`, `mrp`, `pack_size`, `selling_rate`, and/or `stock`.
3. `POST /admin/imports/products` with the workbook, JSON `mapping` form value, optional `brand_override`, and optional `header_row`.
4. Review the created/updated/skipped/failed counts and row errors. Download a saved error report at `/admin/imports/{batch_id}/errors.xlsx`.

The stable product code is required. New products also need a name and a mapped brand or explicit brand override; existing products can be updated from a stock-only file by mapping just product code and stock. Blank cells on re-import leave existing values untouched. Existing product codes update rather than create duplicate products.

`GET /admin/products/template.xlsx` returns a blank workbook template; `GET /admin/products.xlsx` exports the master. Product imports can also update stock through a mapped `stock` column.

### Parties, salespeople and permissions

- In the admin dashboard, preview a party or product workbook, choose its worksheet/header row, map detected columns, and import/update the list in bulk. Review per-row created/updated/skipped/failed results and download an error workbook when rows fail. The equivalent protected APIs are `POST /admin/imports/preview`, `POST /admin/imports/parties`, and `POST /admin/imports/products`.
- Import party masters by mapping stable `party_code` and `name` columns. Product imports use `product_code` as the stable key. Re-uploading updates matching records rather than duplicating them; unmapped/blank values do not clear existing values. Preview detects headers and supports title rows; worksheet index is zero-based and a header row can be overridden.
- Use the party/product template buttons to download templates and the export buttons to retrieve the current full database lists before editing. Imports are admin-only and require the `X-Admin-Key`.
- Generated internal party codes are not ERP/MARG party codes; replace or map them to the actual ERP codes before relying on MARG exports.
- Approve/deactivate a Telegram account and set its discount/free-product permissions through `PUT /admin/salespersons`.
- Import party/brand assignments through `POST /admin/imports/assignments`. New Telegram IDs are created inactive and require explicit approval; brand assignments must match active products and party assignments must match active parties.
- List configured people and assignments with `GET /admin/salespersons`.
- Approve/deactivate an existing account without changing its assignments using `PATCH /admin/salespersons/{telegram_user_id}/approval`.

Salespeople can only select assigned parties and brands. Account approval is by numeric Telegram user ID; display names are not used as identity.

### Orders and exports

Telegram commands: `/order`, `/neworder`, `/cart`, `/switchbrand`, `/back`, `/submit`, `/cancel`, and `/help`. A draft and the conversation state persist in PostgreSQL across service restarts. Cart items can be edited/removed; switching brands retains the cart. Submission is locked to the saved draft and cannot create a second order from a repeated submit.

Use `GET /admin/orders` to search/filter, then `POST /admin/orders/{id}/decision` to approve or reject submitted orders. Daily reports are available at `/admin/reports/daily.xlsx?report_date=YYYY-MM-DD`; the orders endpoint accepts date ranges. Each order has an internal Excel report and PDF summary.

Before MARG export, configure `/admin/marg/mapping` from the actual MARG template. The mapping keys are the real external Excel headers, values are supported internal fields, and `required_fields` comes from the actual import specification. Single-order export is `/admin/orders/{id}/marg.xlsx`. Combined export is available only when explicitly enabled in that mapping and the selected orders are approved. No MARG headers are hard-coded.

## PDF/image order scanner

The Docker image installs Tesseract OCR. Digital PDFs are text-extracted first; image-only PDF pages and JPG/PNG uploads use Tesseract. Local deployments need the Tesseract executable installed and on `PATH`, or `TESSERACT_CMD` set.

The scanner matches exact product codes or unique exact product names and only extracts quantities when a `qty`/`quantity` label is visible. It does not infer a rate, free quantity, discount, ambiguous product, or unlabeled quantity. The sender must explicitly confirm; confirmed certain lines become an editable draft and uncertain/unmatched lines must be entered manually. Uploaded document bytes and OCR text are stored privately in PostgreSQL and cleared after `SCANNER_RETENTION_HOURS` (default 24); choosing **Discard review** clears them immediately. Scanner review APIs are admin-protected.

OCR results require human verification. Handwriting, low-quality images, unusual layouts, and non-English text can require additional OCR configuration; the current scanner is designed to warn or leave a line unmatched rather than silently submit a guess.

## Tests

Run `python -m pytest -q`. Tests cover decimal calculations, Excel header/code handling, duplicate rows, MARG mapping validation, scanner uncertainty, and workbook generation. A live PostgreSQL instance and Telegram token are not needed for these unit tests; production database/webhook integration should be smoke-tested in a non-production Render environment before rollout.
