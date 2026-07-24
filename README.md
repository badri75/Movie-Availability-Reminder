# BookMyShow booking monitor

This project checks BookMyShow for the configured movie at the theatre and date
encoded in `theatre_url`.
A positive result is checked twice, 30 seconds apart, before one Telegram
notification is sent. The notification includes each showtime's format,
availability, ticket class, and current price when BookMyShow supplies them.
Availability wording is preserved from BookMyShow, and prices are displayed
without a currency symbol.

No movie event/listing ID is configured or hard-coded. The monitor opens the
direct, dated theatre booking URL from `config.json` and finds the exact movie
name within that one schedule page. Standard, IMAX, and other available formats
are recognized without needing their separate movie IDs.

When `SCRAPERAPI_API_KEY` is set, the monitor uses `poller.py` to retrieve the
configured theatre schedule through ScraperAPI. It uses a standard, non-premium
request with no paid country-level geotargeting and browser rendering disabled,
so each normal check makes one ScraperAPI page request instead of two.
Without that variable, it falls back to the local Playwright browser. The monitor
does not sign in, choose seats, or purchase tickets.

## Configuration

`config.json` intentionally accepts exactly four fields:

```json
{
  "movie_name": "The Odyssey",
  "theatre_url": "https://in.bookmyshow.com/cinemas/CHEN/inox-luxe-phoenix-market-city-velachery/buytickets/INPR/20260722",
  "formats": "IMAX",
  "bookmyshow_retry_delay_seconds": 90
}
```

`theatre_url` must be a direct HTTPS BookMyShow theatre booking URL. The monitor
derives the venue code and date from this URL and reads the theatre name from the
returned schedule page. A positive result is still checked again after the
confirmation delay before Telegram is notified, so a confirmed positive run
intentionally performs a second one-page check.

Use a semicolon-separated value such as `"2D;IMAX"` to include multiple
formats. Use an empty string, `"formats": ""`, to return every format.

If a BookMyShow check fails with a site/browser error, the monitor waits
`bookmyshow_retry_delay_seconds` and starts that check again once.

Telegram credentials must never be added to that file.

The ScraperAPI key must also remain outside `config.json`. Load it into the
current PowerShell session when testing the ScraperAPI path:

```powershell
$secureScraperApiKey = Read-Host "Enter the ScraperAPI key" -AsSecureString
$scraperApiKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureScraperApiKey)
try {
    $env:SCRAPERAPI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($scraperApiKeyPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($scraperApiKeyPointer)
}
```

The only optional request setting is `SCRAPERAPI_RENDER`, which defaults to
`false`. If BookMyShow does not return usable HTML, browser rendering can be
tested with `$env:SCRAPERAPI_RENDER = "true"`, but it consumes more credits.
The monitor intentionally does not send `premium` or `country_code`, keeping the
default request compatible with ScraperAPI's free tier.

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
Remove-Item Env:SCRAPERAPI_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:SCRAPERAPI_RENDER -ErrorAction SilentlyContinue
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
- `SCRAPERAPI_API_KEY`

The workflow automatically uses `poller.py` when the `SCRAPERAPI_API_KEY` secret
is present. It uses Playwright only when that secret is absent. The only optional
GitHub Actions repository variable is `SCRAPERAPI_RENDER`; leave it unset to use
the free-tier-oriented default.

After Telegram delivery succeeds, the workflow disables itself to prevent repeat
alerts. Scheduled GitHub Actions can be delayed, so a ten-minute schedule is not a
strict timing guarantee.

No repository, secrets, or workflow have been deployed by this project setup.
