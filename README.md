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

## GitHub Actions triggered by cron-job.org

The included workflow uses `workflow_dispatch` only. It does not have an
internal GitHub schedule, so cron-job.org can be the single scheduler without
creating duplicate runs.

Store these values under **Repository settings -> Secrets and variables ->
Actions**:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SCRAPERAPI_API_KEY`

The workflow automatically uses `poller.py` when the `SCRAPERAPI_API_KEY` secret
is present. It uses Playwright only when that secret is absent. The only optional
GitHub Actions repository variable is `SCRAPERAPI_RENDER`; leave it unset to use
the free-tier-oriented default.

Create a fine-grained GitHub personal access token for only this repository.
Give it **Actions: Read and write** repository permission. Do not use a GitHub
password or put this token in the repository.

Create a cron job at cron-job.org with these request settings:

- URL:
  `https://api.github.com/repos/badri75/Movie-Availability-Reminder/actions/workflows/bms-monitor.yml/dispatches`
- Request method: `POST`
- Request body: `{"ref":"master"}`
- Header `Accept`: `application/vnd.github+json`
- Header `Authorization`: `Bearer YOUR_FINE_GRAINED_GITHUB_TOKEN`
- Header `X-GitHub-Api-Version`: `2026-03-10`
- Header `Content-Type`: `application/json`

Set the cron-job.org timezone to `Asia/Kolkata`. An hourly schedule, for example
at minute 7 of every hour, performs about 720 normal checks in a 30-day month and
is suitable for ScraperAPI's 1,000-credit free allowance when each check costs
one credit. A ten-minute schedule performs about 4,320 normal checks per month
and therefore does not fit that allowance. A confirmed positive result performs
one additional check.

Use cron-job.org's test-run option after saving. With the configured GitHub API
version, a successful dispatch returns HTTP `200` with workflow-run details; the
actual monitor then runs asynchronously in the repository's **Actions** tab.

After Telegram delivery succeeds, the workflow disables itself to prevent repeat
alerts. Disable the cron-job.org job as well after receiving the notification;
otherwise its later dispatch requests will fail because the GitHub workflow is
disabled.

No repository, secrets, or workflow have been deployed by this project setup.
