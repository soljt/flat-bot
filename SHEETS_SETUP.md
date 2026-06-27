# Google Sheets Setup

The bot can append each new match to a shared Google Sheet with a "Human Sent Message" column your group fills in once you've reached out. This is optional — the bot works fine without it.

## 1. Create a Google Cloud project

1. Go to https://console.cloud.google.com and sign in.
2. Click the project dropdown (top left) → **New Project**.
3. Give it a name (e.g. `flatbot`) and click **Create**.

## 2. Enable the Google Sheets API

1. In your new project, go to **APIs & Services → Library**.
2. Search for **Google Sheets API** and click **Enable**.
3. Search for **Google Drive API** and click **Enable** (required by gspread for file access).

## 3. Create a service account

1. Go to **APIs & Services → Credentials → Create Credentials → Service Account**.
2. Name it `flatbot-writer`, click through the optional steps, then **Done**.
3. Click the newly created service account → **Keys** tab → **Add Key → Create new key → JSON**.
4. A JSON file downloads. Rename it `service-account.json` and place it in the `flat-bot/` project directory (it's gitignored).

## 4. Create the Google Sheet

1. Go to https://sheets.google.com and create a new blank spreadsheet.
2. Name it `FlatBot Matches` (or anything you like).
3. Copy the **Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
   ```

## 5. Share the sheet with the service account

1. Open the service account JSON file and copy the `client_email` value (looks like `flatbot-writer@your-project.iam.gserviceaccount.com`).
2. In Google Sheets, click **Share**, paste that email address, and grant **Editor** access.

## 6. Set the env vars

Add these two lines to your `.env`:

```
GOOGLE_SHEETS_ID=<paste the Sheet ID from step 4>
GOOGLE_SERVICE_ACCOUNT_JSON=service-account.json
```

The bot will automatically write a header row the first time it runs, then append one row per new match:

| Seen At | Platform | ID | Title | URL | Rooms | Rent (CHF) | Postcode | Address | Available From | Flags | Human Sent Message |
|---|---|---|---|---|---|---|---|---|---|---|---|

Fill in the **Human Sent Message** column manually once you've reached out about a listing.
