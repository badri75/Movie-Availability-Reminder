# BookMyShow booking monitor

This project checks BookMyShow for the movie, theatre, and date in `config.json`.
A positive result is checked twice, 30 seconds apart, before one Telegram
notification is sent. The notification includes each showtime's format,
availability, ticket class, and current price when BookMyShow supplies them.
Availability wording is preserved from BookMyShow, and prices are displayed
without a currency symbol.

No movie event/listing ID is configured or hard-coded. On every check, the
monitor finds the exact theatre in BookMyShow's city cinema catalogue and opens
that theatre's schedule for the configured date. It then finds the exact movie
name within the schedule, so standard, IMAX, and other available formats are
recognized without needing their separate movie IDs.

The monitor does not sign in, choose seats, bypass CAPTCHAs, or purchase tickets.
It blocks images, media, and fonts to keep each check lightweight.

## Configuration

`config.json` intentionally accepts exactly four fields:

```json
{
  "movie_name": "The Odyssey",
  "city": "Chennai",
  "theatre_name": "PVR: Palazzo, The Nexus Vijaya Mall",
  "date": "2026-07-22"
}
```

Telegram credentials must never be added to that file.

## Local setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
playwright install chromium
```

If a corporate TLS proxy prevents the Playwright browser download, use an existing
local Chrome installation instead:

```powershell
$env:BMS_BROWSER_CHANNEL = "chrome"
```

With that variable set, `playwright install chromium` is not required locally.
GitHub Actions leaves the variable unset and uses Playwright's bundled Chromium.

Generate a **new** Telegram bot token through BotFather. Do not reuse a token that
has appeared in chat or logs.

Load the replacement token into the current terminal without displaying it or
putting it in command history:

```powershell
$secureToken = Read-Host "Enter the replacement Telegram bot token" -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
}
$env:TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"
```

Run a BookMyShow check without contacting Telegram:

```powershell
python monitor.py --dry-run --confirmation-delay 0
```

Send an explicit Telegram connectivity test:

```powershell
python monitor.py --test-notification
```

Run a real check and notify when confirmed:

```powershell
python monitor.py
```

After testing, clear the session credentials:

```powershell
Remove-Item Env:TELEGRAM_BOT_TOKEN
Remove-Item Env:TELEGRAM_CHAT_ID
Remove-Item Env:BMS_BROWSER_CHANNEL -ErrorAction SilentlyContinue
```

`state.json` is created only after successful delivery and prevents duplicate local
notifications. It is excluded from Git.

## Tests

Tests do not access BookMyShow or Telegram:

```powershell
python -m unittest discover -s tests -v
```

## GitHub Actions (not deployed)

The included workflow checks every ten minutes at minutes 7, 17, 27, 37, 47,
and 57. Store the replacement values under **Repository settings → Secrets and
variables → Actions** as:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

After Telegram delivery succeeds, the workflow disables itself to prevent repeat
alerts. Scheduled GitHub Actions can be delayed, so a ten-minute schedule is not a
strict timing guarantee.

No repository, secrets, or workflow have been deployed by this project setup.
