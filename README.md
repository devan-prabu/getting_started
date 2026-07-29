# UAE Project Radar

Watches public sources for UAE construction contract awards and alerts you with
the resulting **hiring window**, so you can approach a contractor during
mobilisation — before the job is posted.

The contract award is a public event. The hiring is a predictable consequence of
it, typically 4–10 weeks later. This tool detects the first and computes the
second.

**Status: Phase 0.** Google News RSS only, rough regex extraction, working alert
loop. See [Phases](#phases) for what is and is not built.

---

## ⚠️ Read this before your first run

Nothing in this repo has been verified against the live internet. It was built
in a sandbox whose network policy blocks every host except package registries and
GitHub, so no feed URL was ever fetched and no Telegram message was ever
delivered. Every source is therefore marked `status: unverified` rather than
falsely marked working. Full detail in [DECISIONS.md](DECISIONS.md) (D-01).

Your first job on a machine with normal internet is:

```bash
radar sources check
```

That fetches every source, parses the response, and rewrites `status`,
`last_checked` and `notes` in `config/sources.yml` to `live` or `dead` with a
reason. **One thing to watch:** if `news.google.com/robots.txt` disallows
`/rss/`, every Google News source will be skipped and logged. That is correct
behaviour — robots.txt is obeyed with no override flag — and `sources check`
will tell you so explicitly rather than failing quietly.

---

## Setup

Requires Python 3.11+.

```bash
git clone <this repo> && cd uae-project-radar
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
$EDITOR .env          # at minimum, set CONTACT_EMAIL

radar init            # create the DB and load companies.yml
radar sources check   # verify the feeds actually work
radar run --dry-run   # full pipeline, alerts printed to stdout
```

`radar run --dry-run` records nothing to the alert ledger, so you can run it as
often as you like while tuning the queries.

### Turning on Telegram

1. Message [@BotFather](https://t.me/botfather), `/newbot`, copy the token.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[0].message.chat.id`.
4. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. `radar alert` (no `--dry-run`).

### Turning on email

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` and
`EMAIL_TO` in `.env`. Use an app-specific password, never your account password.

`ALERT_CHANNELS` selects which channels are live, e.g. `telegram,email` or just
`telegram`. Each channel tracks delivery separately, so enabling email later will
not re-send the Telegram backlog.

---

## Commands

| Command | What it does |
|---|---|
| `radar init` | Create the DB and load `companies.yml` into the `companies` table |
| `radar collect [--source ID]` | Fetch feeds into `raw_items`, deduped |
| `radar enrich [--limit N]` | Pull clean article text with trafilatura |
| `radar filter` | Apply the keyword gate |
| `radar extract [--limit N]` | Rough regex extraction into `awards` |
| `radar alert [--dry-run]` | Send unsent awards on every configured channel |
| `radar run [--dry-run]` | collect → enrich → filter → extract → alert |
| `radar sources check` | Verify every source; rewrite status in `sources.yml` |
| `radar sources list` | Show the source registry without touching the network |
| `radar status` | Row counts by table and item status |
| `radar resolve-url URL` | Show what a Google News link resolves to |

**Every command is safely re-runnable.** `radar run` twice in a row produces zero
duplicate rows and zero duplicate alerts; this is enforced by a unique index on
the canonicalised URL hash and by `UNIQUE(award_id, channel)` on `alerts_sent`,
and it is covered by tests in `tests/test_pipeline.py`.

---

## How it works

```
sources.yml → COLLECT → DEDUPE → ENRICH → FILTER → EXTRACT → SQLite → alert
              feeds     url+title trafila-  keyword  regex
                        hashing   tura      gate     (Phase 0)
```

`raw_items` is the durable log. Everything downstream is derived and
re-computable, so a failure in extraction never loses a collected article.

Two things are harder than they look and are solved in `dedupe.py`:

- **Google News redirect resolution.** Feed links are `news.google.com`
  redirects. Hashing those instead of the publisher URL stores the same article
  once per query that surfaced it. The article id is base64-decoded offline and
  the publisher URL read from it, with redirect-following as a capped fallback.
- **One award, six outlets.** URL dedupe cannot catch the same story reported
  separately. Titles are compared with rapidfuzz `token_set_ratio > 88` within a
  72-hour window, ignoring the `- Publisher` suffix.

---

## Configuration

- `config/sources.yml` — feeds. Google News entries store a `query`; the URL is
  built and encoded by the code, never hand-written.
- `config/companies.yml` — canonical names, aliases, careers URLs, tier,
  watchlist flag, parent company. Loaded into the DB by `radar init`. Phase 0
  does not match against it yet; Phase 2 does.
- `config/keywords.yml` — the award/UAE/exclusion term lists for the cheap gate.
  Pass requires ≥1 award signal **and** ≥1 UAE signal **and** 0 exclusions.
  Expect ~15% to pass; the rate is logged every run and warns if it drifts.

---

## Ethics

These are hard constraints, not preferences, and they live in `fetcher.py` with
tests in `tests/test_fetcher.py`:

- **robots.txt is obeyed.** Disallowed path → skip and log. There is no override
  flag and none will be added.
- **≥3 seconds between requests to the same host.** This tool has zero urgency.
- **Honest User-Agent with a contact address.** No browser impersonation, no
  rotating UAs, no proxies.
- **No paywalled sources.** MEED, Zawya Projects, BNC Network and ProTenders are
  out of scope, including via archive mirrors.
- **No login-gated content**, no credential storage.
- **Facts, not prose.** `scope_summary` is capped at 200 characters and alerts
  always link to the source so the publisher gets the traffic.
- **Personal use.** This is not a redistribution feed for other people's
  reporting.

---

## Testing

```bash
pytest              # 233 tests, no network access of any kind
ruff check . && ruff format --check .
```

Every module in `src/radar/` has a matching test file. All HTTP is either
intercepted with `respx` or served from `tests/fixtures/`. Those fixtures are
hand-built rather than recorded — see `tests/fixtures/README.md` for what that
means and when to replace them.

---

## GitHub Actions

`.github/workflows/radar.yml` runs every 6 hours and on manual dispatch. It
commits `data/radar.db` back to the repo after each run so state survives between
firings, checkpointing the WAL first and retrying the push if a queued run got
there first. A concurrency group prevents two runs writing the DB at once, and a
failed run sends a Telegram message rather than failing silently.

Secrets to set: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CONTACT_EMAIL`, and
the `SMTP_*` / `EMAIL_*` pair if you want email. Set the `ALERT_CHANNELS`
repository variable to pick channels.

---

## Phases

**Phase 0 — working alert loop (done, this repo)**
Google News RSS, URL and title dedupe, trafilatura enrichment, keyword gate,
rough regex extraction, Telegram + email alerts, fixture-based tests.

**Phase 1 — source breadth**
Verify and populate `sources.yml`, newsroom pollers for developers and
contractors, at least one exchange-disclosure source (ADX or DFM), source health
reporting, filter pass rate tuned to ~15%.

**Phase 2 — structured extraction**
LLM extractor behind a swappable interface, pydantic validation with one repair
retry, `normalise.py` fuzzy-matching contractors against `companies.yml`.

**Phase 3 — the payoff**
`score.py` producing relevance and the hiring window, alerts carrying the careers
URL and the hiring-window sentence, `radar report`, watchlist-only mode.

**Phase 4 — polish**
Streamlit dashboard, weekly digest, job-posting spike detection.
