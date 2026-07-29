# Decisions

Every judgement call, with the reasoning, so future phases do not relitigate
them. Newest last.

---

## D-01 — Nothing in this repo has been verified against the live internet

**Phase 0. This is the most important entry in the file.**

The brief (§0.2) says never to write a URL into `sources.yml` without fetching
it first. The sandbox this repo was built in has an egress policy that allows
only package registries and GitHub; `news.google.com`, ADX, DFM, every newsroom
and `api.telegram.org` all return `403` at the proxy. Routing around an
organisation's egress policy is not something to attempt, so no URL was fetched.

What that means concretely:

- Every entry in `config/sources.yml` is `status: unverified` with
  `last_checked: null`. **None of them claims to work.** A dishonest `live` would
  have been a failure per §0.2; `unverified` is the truthful state.
- Every `careers_url` in `config/companies.yml` is an unverified candidate.
- The test fixtures in `tests/fixtures/` are hand-built from the documented
  shapes of the formats, not recorded from live traffic. See
  `tests/fixtures/README.md`.
- **Telegram delivery to a phone has not been demonstrated.** The send path is
  covered by tests against a mocked Bot API, which proves the request is
  well-formed, not that a message arrives.

`radar sources check` exists to close this gap in one command on a machine with
normal internet access. It fetches every source, parses the response, and
rewrites `status`, `last_checked` and `notes` in place.

**Open risk worth checking first:** if `news.google.com/robots.txt` disallows
`/rss/`, the fetcher will skip every Google News source and log it. That is the
correct behaviour under §9.1 and there is deliberately no override flag. Run
`radar sources check` before assuming anything is broken — it will say so
explicitly.

## D-02 — Both Telegram and email alert channels (asked)

Answered by the user: build both. `alerts_sent` has `UNIQUE(award_id, channel)`,
so each channel tracks its own delivery and enabling a second channel later does
not re-alert the backlog on the first. `ALERT_CHANNELS` in `.env` selects which
are live. Telegram uses a plain `POST` to the Bot API (no SDK); email uses
stdlib `smtplib`.

## D-03 — Claude API as the extraction backend (asked)

Answered by the user: Claude API. Nothing is wired up yet — Phase 0 has no LLM
stage — but `.env.example` defaults `EXTRACTOR_BACKEND=claude` and Phase 2 will
build the swappable `Extractor` protocol around it. The accuracy matters most on
exactly the failure the brief flags in §13.3 (contractor vs developer), which is
where a local 7B model struggles most.

## D-04 — A `config.py` module that the brief does not list

`collect`, `filters` and `cli` all need to read the same YAML files. Parsing them
in three places would drift. `SourcesConfig` also owns URL building and the
in-place status rewrite that `radar sources check` needs.

## D-05 — Google News URLs are built from a `query` field, never hand-written

`sources.yml` stores the query string; `SourcesConfig.build_url` URL-encodes it
against `google_news_base`. Hand-writing six nearly identical percent-encoded
URLs invites a typo that silently returns nothing, and §0.2 forbids writing a URL
that has not been verified — a generated one at least cannot be mistyped.

## D-06 — A minimal `extract.py` in Phase 0

The brief's Phase 0 step list (§14) does not include `extract.py`, but
`alerts_sent.award_id` is a foreign key to `awards.id`, so the "exactly once per
channel, ever" guarantee has nothing to hang on without award rows. Rather than
weaken that guarantee, Phase 0 ships the rough regex extraction that §10
explicitly sanctions, writing real `awards` rows.

It is deliberately incapable of looking confident: `MAX_REGEX_CONFIDENCE = 0.45`
caps every record it produces, and the alert says "contractor guessed by regex"
below 0.5. Phase 2 replaces the body of `extract_from_text` with the LLM behind
an `Extractor` protocol; the table, the alert path and the idempotency guarantees
are already exercised, so that swap is the only change needed.

`score.py`, `normalise.py` and `report.py` were **not** built — those are Phases
2 and 3, and §0.1 says not to scaffold future phases early.

## D-07 — The brief's keyword list is shipped unchanged, with one known gap

`"wins contract"` is a fixed phrase, so the very common headline shape
`"NMDC wins Abu Dhabi marine works package"` does not match it. Loosening it to
bare `"wins"` would pull in every sports and awards story, so it was left alone:
such items are usually rescued by the body text saying "awarded", and §6.3 says
to tune against a measured pass rate rather than guess. `filters.filter_enriched`
logs the pass rate every run and warns outside 5–25%. Phase 1 should revisit this
with real numbers. Test:
`test_filters.py::test_known_gap_split_wins_contract_phrasing`.

## D-08 — DB gitignored locally, committed by CI (asked)

Answered by the user: the brief's default (§8a). `.gitignore` excludes
`data/radar.db` so local runs do not dirty the tree; the workflow commits it back
with an explicit `git add -f` after each run so state survives between cron
firings. The commit step runs `if: always()` — a failed pipeline must not discard
an already-collected batch, because `raw_items` is the durable log (§2). The WAL
is checkpointed before committing so the file is self-contained, and the push
retries with backoff in case a queued run pushed first.

## D-09 — Google News ids are decoded offline before any redirect is followed

§13.1 calls redirect resolution the number one source of duplicate rows. Doing it
purely by following redirects would mean one throttled request per item — at the
3s/host floor, roughly five minutes per 100-item query. So `dedupe` first
base64-decodes the article id and reads the publisher URL straight out of the
protobuf blob, which costs nothing and covers the older id format. Following
redirects is the fallback, capped at `--max-resolve` (default 50) per run, and
keeping the unresolved Google URL is the last resort — losing the item would be
worse.

The decoder reads the protobuf **length prefix** rather than regex-matching
greedily to the end of the URL. This was a real bug caught in testing: a greedy
match swallows the next field's tag byte, producing a URL one character too long
that then hashes to something no other copy of the same article will ever match —
silently defeating the deduplication it exists to provide.

## D-10 — `parse_feed` rejects a response that is not a feed at all

An HTML error page (`429 Too Many Requests`, a captcha interstitial) parses
through `feedparser` without complaint and yields zero entries, which is
indistinguishable from a healthy-but-empty feed. A dead source would have looked
fine in `source_health` forever. `parse_feed` now raises unless
`feedparser` recognised a feed version.

## D-11 — Trivially short extractions count as no text

trafilatura returns `"Subscribe"` for a paywall stub. Storing that is worse than
storing nothing: it looks like a successful enrich while carrying no signal.
Anything under 200 characters is treated as no text, and the item keeps its title
so the keyword gate can still see it.

## D-12 — Two columns beyond the brief's DDL

`raw_items.language` (default `'en'`) because §13.5 asks that language be a
field, not an assumption, so Arabic sources can be added without a schema
rewrite; and `companies.parent` because §13.6 notes that parent/subsidiary
routing matters (Trojan hires under Trojan General Contracting, not Alpha Dhabi).
Both are added by an additive migration in `db._migrate`, which only ever adds
missing columns.

## D-13 — `--dry-run` records nothing

Previewing an alert must not consume it. In dry-run, `send_alerts` swaps in the
stdout channel and writes no `alerts_sent` row, so the preview can be repeated
and the real send still delivers everything afterwards.

## D-14 — A failed send is not recorded

`alerts_sent` is written only after the channel returns successfully. A Telegram
outage therefore re-alerts on the next run rather than silently swallowing an
award. The cost is a possible duplicate if a send succeeds but the process dies
before the commit — a duplicate alert is a far better failure than a missed one.
