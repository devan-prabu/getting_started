# UAE Project Radar + interview drill agent

Two tools that share a repo, a Telegram bot and a set of conventions:
**[the radar](#uae-project-radar)** watches for contract awards and the hiring window they
imply, and **[the drill agent](#the-interview-drill-agent-prep)** makes sure you can win the
interview when one opens.

---

## UAE Project Radar

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
pytest              # 441 tests, no network access of any kind
pytest tests/test_prep_*.py   # the drill agent alone (205)
ruff check . && ruff format --check .
```

Every module in `src/radar/` and `src/prep/` has a matching test file. All HTTP
is either intercepted with `respx` or served from `tests/fixtures/`. Those
fixtures are hand-built rather than recorded — see `tests/fixtures/README.md`
for what that means and when to replace them.

**Three radar tests currently fail, and they were failing before the drill agent
was added.** `tests/fixtures/google_news_*.xml` carry hardcoded July 2026
`pubDate` values, and title-similarity dedupe only looks inside a 72-hour window
— so as real time moved past those dates, the fixture items dropped out of the
window and stopped being compared. It is a stale-fixture problem, not a bug in
`dedupe.py`: the fix is to generate the fixture dates relative to now, or to
freeze the clock in those tests.

---

## GitHub Actions

Two workflows, each with its own concurrency group and its own state file.

`.github/workflows/prep.yml` runs every 2 hours and on manual dispatch (with a
`force_ask` input). Each run polls your Telegram replies, grades them, adapts
the context and sends a drill if it is past your local ask hour and none has
gone out today. It commits both `data/prep.db` and `config/profile.yml` back,
since the adaptive block is state too. Secrets: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, and `ANTHROPIC_API_KEY` if you want generated questions
rather than the bank.

`.github/workflows/radar.yml` runs every 6 hours and on manual dispatch. It
commits `data/radar.db` back to the repo after each run so state survives between
firings, checkpointing the WAL first and retrying the push if a queued run got
there first. A concurrency group prevents two runs writing the DB at once, and a
failed run sends a Telegram message rather than failing silently.

Secrets to set: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CONTACT_EMAIL`, and
the `SMTP_*` / `EMAIL_*` pair if you want email. Set the `ALERT_CHANNELS`
repository variable to pick channels.

---

# The interview drill agent (`prep`)

The radar tells you **when** a job is about to exist. This tells you whether you
can win the interview when it does.

`prep` sends you a few interview questions on Telegram every evening, grades the
answers you type back from your phone, and **rewrites its own context** as it
learns what you keep getting wrong and where you are aiming.

It is aimed at one specific move: contractor-side QA/QC at Proscape → **QA/QC
Engineer on the consultancy / PMC supervision side, or a tier-1 contractor**, at
a package that beats the current one. That target lives in `config/profile.yml`
and is yours to change at any time — from the file or from the phone.

```
profile.yml ─┐
             ├─→ system prompt ─→ QUESTION ─→ Telegram ─→ your answer
topic_state ─┘         ▲                                        │
                       │                                        ▼
                    ADAPT ←───────── score, weak topics ──── GRADE
```

## Setup (five minutes)

```bash
pip install -e ".[dev]"
cp .env.example .env      # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY if you have one
$EDITOR config/profile.yml   # fill in the TODO lines — years, salary, notice period
prep init
prep ask --dry-run --bank    # see tonight's drill without sending anything
```

Then either run `prep run` from cron, or let
[`.github/workflows/prep.yml`](.github/workflows/prep.yml) do it — it fires
every two hours, sends one drill after your local ask hour, and commits the
state back.

**It works with no Anthropic key.** `config/topics.yml` ships ~60 real questions
across 15 topics, and grading falls back to answer-key coverage. The key buys
you questions written fresh against your profile and honest prose feedback; it
does not buy you the difference between the agent running and not running.

## Using it from your phone

Answer by **replying** to a question — that is how an answer gets attached to
the right question. Everything else is a slash command:

| Command | What it does |
|---|---|
| `/ask` · `/again` | Send a drill now · resend what is still open |
| `/skip` · `/score 70` | Give up on one (counts as a miss) · grade yourself |
| `/goal <text>` | "I am chasing the KEO landscape inspector role now" |
| `/target <company>` · `/cv <text>` | A company you are chasing · something new on your CV |
| `/focus <topic>` · `/drop <topic>` | Drill this harder · park it |
| `/harder` · `/easier` | Move the difficulty dial |
| `/context` · `/status` | What it is running on · your scores and weak spots |
| `/topics` · `/pause 7` · `/resume` | Topic ids · stop for a week · restart |

## How it corrects its own context

This is the part that matters, and it is not a prompt that says "adapt".

**`config/profile.yml` is split in two.** Above `adaptive:` is yours — identity,
target role, salary floor, strengths, gaps. Nothing the agent does ever writes
there. Below it is the machine-maintained block, and `prep adapt` rewrites it
from three inputs, in this order of authority:

1. **What you told it.** `/goal`, `/focus`, `/harder` and friends are queued as
   events and folded into the context on the next adapt. You outrank the data.
2. **What you scored.** Topics averaging under 60 are pushed into `focus`,
   weakest first. Topics scoring 85+ twice in a row are parked in `resting` —
   asked occasionally, not never, because an interviewer opens on your strongest
   ground. The difficulty dial follows your 21-day rolling average, one notch at
   a time.
3. **Where you are in time.** As `interview_window_from` approaches, behavioural,
   HR and company-specific questions climb above deep technical ones. Stop
   answering for ten days and it eases off rather than piling up.

The system prompt is then **assembled** from that — never hand-written, never
stale — and hashed. A changed hash writes a new row in `context_versions`, and
every question is stamped with the edition that produced it:

```bash
prep context             # what it believes right now
prep context --full      # the entire system prompt
prep context --history   # every edition, and why it changed
```

An unchanged prompt writes nothing, so the edition number means "the context
genuinely moved", not "the job ran again".

## Commands

| Command | What it does |
|---|---|
| `prep init` | Create `data/prep.db` and record the first context edition |
| `prep ask [--force] [--dry-run] [--bank] [-n N]` | Send today's drill |
| `prep poll` | Read replies and commands from Telegram |
| `prep grade [--no-send]` | Grade what arrived, push the verdicts back |
| `prep adapt [--dry-run]` | Rewrite the adaptive block and the system prompt |
| `prep run [--dry-run] [--bank]` | poll → grade → adapt → ask. The cron path |
| `prep answer "..."` · `prep self-score 75` | Drill from the terminal instead |
| `prep goal "..."` | Change the goal without the phone |
| `prep status` · `prep topics` · `prep context` | Where you are · topic ids · the live context |

`prep run` is safe to run as often as you like: `deliveries` is unique per
question and channel, and the drill will not send twice in one local day.

## Tuning the questions

- **`config/topics.yml`** — the 15 topics, their weights, and the banked
  questions. Each seed carries an `expect` list, which is both the offline
  grading key and the rubric given to the model. Add your own; a seed with no
  `expect` fails the test suite on purpose.
- **`config/profile.yml`** — the `gaps:` list is the fastest lever. Anything you
  put there gets drilled until your scores say otherwise.


---

## Phases (radar)

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
