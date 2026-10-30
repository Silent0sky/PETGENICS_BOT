# Petgenics Bot: Free Test Deployment Guide

This guide deploys a **test** bot using a Render Free web service and Neon Free PostgreSQL. You do not need to edit Python code. This is not a production setup: free services can sleep, have usage limits, and do not provide production uptime/recovery guarantees.

## Before you start

Have these ready:

- A GitHub account with access to [`Silent0sky/PETGENICS_BOT`](https://github.com/Silent0sky/PETGENICS_BOT).
- A Render account and a Neon account. Choose only the **Free** plans; review current [Render Free limits](https://render.com/docs/free) and [Neon prices/limits](https://neon.com/pricing).
- A Telegram bot token from BotFather. **The token previously shared in chat should be revoked. Create a replacement and never send it in chat or put it in a file.**
- Your real party codes from your ERP, plus your product Excel file.

The GitHub repository is currently public. To make the source private, open the repository, go to **Settings → General → Danger Zone → Change repository visibility → Make private** before deploying. Do not upload `.env`, passwords, bot tokens, customer files, or Excel workbooks to GitHub.

## 1. Check the code on GitHub

1. Open [`https://github.com/Silent0sky/PETGENICS_BOT`](https://github.com/Silent0sky/PETGENICS_BOT).
2. Select the `main` branch.
3. Check that you can see `render.yaml`, `Dockerfile`, and `README.md`.

The application code is already pushed. Do not upload the `.env` file or the source Excel files.

## 2. Create a free Neon database

1. Sign up at [Neon](https://neon.com/) and create a project on the **Free** plan.
2. Choose the Singapore region if offered; it is closest to Render's Singapore region.
3. Create/use the default database and role.
4. Open **Connect**, select the database and role, turn on **Connection pooling**, and copy the connection string. It should start with `postgresql://` and commonly includes `sslmode=require` and `channel_binding=require`.
5. Keep this connection string private. Do not paste it in chat, screenshots, GitHub, or this guide.

## 3. Create the free Render web service

1. Sign in at [Render](https://render.com/).
2. Click **New → Web Service** and connect GitHub if asked.
3. Select `Silent0sky/PETGENICS_BOT`, branch `main`.
4. Choose **Docker** as the runtime and leave **Root Directory** blank.
5. Select the **Free** instance plan. Check the displayed price says `$0/month`.
6. Do **not** create a Render Postgres database for this setup. This project’s `render.yaml` now describes the Free web service without provisioning a paid Render database; you can also deploy the Web Service manually.
7. Before deploying, add the initial environment variables in step 4 below. Leave `BOT_TOKEN` and `WEBHOOK_URL` unset for now; the app can start without Telegram while you get its public URL.
8. Click **Deploy Web Service**.

On startup the Docker image applies the database migration, then starts the web app. Render Free services can sleep after 15 minutes without traffic and usually take about a minute to wake.

## 4. Set initial environment variables before deploying

On the service-creation form, enter these in **Environment Variables**. If the service has already been created, open the service → **Environment** instead.

| Variable | What to set |
|---|---|
| `DATABASE_URL` | Paste the private Neon connection string. The app converts Neon’s PostgreSQL URL and SSL options for its async driver. |
| `ADMIN_API_KEY` | Click **Generate** in Render; keep it private. This unlocks admin operations. |
| `ADMIN_TELEGRAM_IDS` | Optional at first. After the new Telegram admin feature is deployed, send `/myid` to the bot and put that numeric ID here to enable `/admin`. |
| `REPORT_UTC_OFFSET_MINUTES` | Optional; defaults to `330` (India Standard Time). Set another UTC offset in minutes only if reports should use a different fixed timezone. |
| `HEALTH_CHECK_TOKEN` | Click **Generate** in Render; keep it private. |
| `WEBHOOK_SECRET` | Click **Generate** in Render; keep it private. |

Do not set `BOT_TOKEN` or `WEBHOOK_URL` yet. Save the environment and deploy the service. It needs the database and admin keys at its first startup.

## 5. Start and connect the Telegram bot

1. Open the Render service's **Events** or **Logs** and wait for **Live**.
2. Open `https://YOUR-SERVICE-URL/healthz` using the exact service URL shown by Render. It should return `{"status":"ok"}`.
3. In Render → **Environment**, add `WEBHOOK_URL` with that exact HTTPS service URL.
4. Add `BOT_TOKEN` using the replacement token from BotFather. Revoke the token previously shared in chat; never paste tokens into this chat.
5. Save changes and wait for Render to redeploy.

Never put secrets in `render.yaml`, GitHub, screenshots, support messages, or this guide. If the token is exposed, revoke it with BotFather and replace it in Render.

## 6. Check the service and open admin

1. Confirm `https://YOUR-SERVICE-URL/healthz` returns `{"status":"ok"}`.
2. Visit `https://YOUR-SERVICE-URL/admin`.
3. Copy `ADMIN_API_KEY` from Render's Environment page and enter it in the admin page.
4. If deployment fails, check Render logs for:
   - `DATABASE_URL` missing or database not ready.
   - Invalid `BOT_TOKEN`.
   - `WEBHOOK_URL` not matching the Render service's HTTPS URL.
   - Neon SSL or connection-string errors (use the full Neon pooled connection string).

The admin page is public, but admin data-changing API requests require the admin key. If you change environment values, wait for the service to restart.

## 7. Import parties and products

Use the protected admin page at `/admin`:

1. **Parties:** use a workbook with your actual ERP party code and party name. Click **Preview and map columns**, check the worksheet and detected header, map the party code and name, then click **Import/update parties**.
2. **Products:** select your product workbook, click **Preview and map columns**, and map product code, product name, brand/company, MRP, pack size, selling rate, and stock where those columns exist.
3. Review the created, updated, skipped, and failed rows. Download the row error report if the page offers one.
4. Use the template and export buttons to get blank templates or download the current database lists.

**Important:** use stable ERP party and product codes. The previously prepared `PARTIES_IMPORT_READY.xlsx` uses generated internal party IDs, not real ERP/MARG codes. Do not use those IDs for MARG export unless you deliberately reconcile them with the ERP.

Blank cells do not clear existing values. Re-importing the same code updates that record instead of making a duplicate.

## 8. Use the Telegram admin menu

After the updated version is deployed:

1. Send `/myid` to your bot and note the numeric ID it returns. Do not share it publicly.
2. In Render → your service → **Environment**, add `ADMIN_TELEGRAM_IDS` with that number as its value, then save and wait for restart.
3. Send `/admin` or tap **Admin panel**. Use the Telegram command menu and persistent reply keyboard as shortcuts; the inline buttons open paginated admin workflows for orders, people, catalogue, reports, imports, scanner runs, and MARG settings.
4. Download the product, party, or assignment templates from the bot before filling them. Use stable party/product codes and exact brand names. New salespeople remain inactive until you approve them.
5. To download a date-wise Excel report, send `/adminreport YYYY-MM-DD`, for example `/adminreport 2026-10-29`. Reports use India Standard Time by default (`REPORT_UTC_OFFSET_MINUTES=330`).

The web dashboard and protected APIs remain available as a fallback. MARG export requires configuration from the actual MARG template; do not guess its columns.

## 9. Test the Telegram bot

1. In Telegram, open the bot created with BotFather and send `/start`.
2. If the salesperson is approved and has assignments, try `/order`.
3. Submit a small test order and check that it appears in the admin page.
4. If Telegram does not respond, check that `BOT_TOKEN` is valid, `WEBHOOK_URL` is the exact public HTTPS service URL, and the Render service is **Live**.

The bot cannot create an order until you import at least one active party and product, then configure an approved salesperson with that party and product brand.

## 10. Backups and future code updates

- Neon Free has limited usage and recovery features. Export important test data and do not rely on the free database as the only copy of business records. Product lists, parties, orders, and drafts are stored in PostgreSQL, not on the temporary Render web-service disk.
- To update code, make the changes locally, run `python -m pytest -q`, commit and push them to the GitHub `main` branch. Render will deploy the new commit automatically if auto-deploy is enabled.
- Before a production launch, test the bot, imports, approvals, order submission, and database recovery with non-production data.

## Quick links

- [GitHub project](https://github.com/Silent0sky/PETGENICS_BOT)
- [Render dashboard](https://dashboard.render.com/)
- [Full project README](./README.md)
