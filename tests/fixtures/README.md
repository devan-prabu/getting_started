# Test fixtures

Recorded inputs so the test suite never touches the network (brief §0.4).

**These are hand-built, not recorded from live traffic.** The sandbox this repo
was created in had no egress to `news.google.com` or any news site (see
DECISIONS.md D-01), so real responses could not be captured. They are modelled on
the documented shapes:

- `google_news_awards.xml` — Google News RSS `search` output: `<link>` values are
  `news.google.com/rss/articles/<base64-id>` with the publisher URL embedded in
  the id, RFC 822 `<pubDate>`, and the `Headline - Publisher` title convention.
  The article ids really do base64-decode to the URLs in
  `test_dedupe.py::test_decode_google_news_url`, so the decoder is exercised for
  real, not stubbed.
- `google_news_duplicate.xml` — the same award reported by two other outlets,
  for the fuzzy title-dedupe test (brief §13.2).
- `google_news_empty.xml` — a well-formed feed with no items.
- `malformed.xml` — not a feed at all; the collector must not crash.
- `article_award.html` / `article_noise.html` / `article_paywall.html` — article
  pages for trafilatura and the keyword gate.
- `robots_allow.txt` / `robots_disallow.txt` — robots.txt bodies.

**Replace these with real captures once you have run the tool with internet
access.** `radar sources check` will tell you whether the live feed still looks
like this; if Google changes the article-id encoding, the offline decoder will
start returning `None` and fall back to following redirects, which is the
intended failure mode rather than a crash.
