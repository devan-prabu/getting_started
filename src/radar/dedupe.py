"""URL canonicalisation, Google News redirect resolution, and fuzzy title dedupe.

Brief §13.1 calls Google News redirect resolution the number one source of
duplicate rows, and §13.2 points out that one award gets reported by six
outlets. Both are solved here, in Phase 0, because every later phase inherits
whatever mess is left.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from rapidfuzz import fuzz

log = logging.getLogger(__name__)

GOOGLE_NEWS_HOSTS = {"news.google.com", "www.news.google.com"}

# Tracking junk that changes per-share but never changes the article.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "cmpid",
    "ncid",
    "oc",
    "guccounter",
}

TITLE_SIMILARITY_THRESHOLD = 88  # rapidfuzz token_set_ratio; brief §5.1
DEDUPE_WINDOW_HOURS = 72

_URL_IN_BYTES = re.compile(rb"https?://[^\x00-\x20\"'<>\\]{8,}")
# Bytes that can never end a URL but often follow one in the protobuf blob.
_URL_TRAILING_JUNK = b".,;:'\")>]}"
# Google News titles carry a " - Publisher" suffix that differs per outlet and
# would otherwise drag the similarity score down for what is the same story.
_TITLE_SUFFIX = re.compile(r"\s+-\s+[^-]{2,40}$")


def strip_tracking_params(query: str) -> str:
    kept = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_PARAMS
    ]
    return urlencode(kept)


def canonicalise(url: str) -> str:
    """Lowercase the host, drop tracking params and fragments, strip trailing /."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlparse(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    # Drop a default port; http://x:80/ and http://x/ are the same page.
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path.rstrip("/") or "/"
    query = strip_tracking_params(parts.query)
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """sha256 of the canonicalised URL. Canonicalise first, always."""
    return hashlib.sha256(canonicalise(url).encode("utf-8")).hexdigest()


def is_google_news_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in GOOGLE_NEWS_HOSTS


def _varint_before(blob: bytes, idx: int) -> int | None:
    """Read the protobuf length prefix sitting just before `idx`.

    Varints are little-endian base-128 with a continuation bit on every byte but
    the last, so a 1-byte prefix is simply a value under 0x80 and a 2-byte one is
    (low & 0x7f) | (high << 7). Anything longer would mean a URL over 16KB, which
    does not happen.
    """
    if idx >= 1:
        one = blob[idx - 1]
        if one < 0x80:
            return one
    if idx >= 2:
        low, high = blob[idx - 2], blob[idx - 1]
        if low >= 0x80 and high < 0x80:
            return (low & 0x7F) | (high << 7)
    return None


def decode_google_news_url(url: str) -> str | None:
    """Recover the publisher URL from a Google News article id, offline.

    The id segment of /rss/articles/<id> is base64 of a small protobuf. For a
    large share of items that blob contains the publisher URL as a plain
    length-delimited string, so we can skip a network round trip entirely.
    Returns None when the id uses the newer opaque form — the caller then falls
    back to following redirects.
    """
    parts = urlparse(url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[-2] not in {"articles", "read"}:
        return None
    token = segments[-1]
    # urlsafe_b64decode is strict about padding; the ids never carry it.
    padded = token + "=" * (-len(token) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None

    for scheme in (b"https://", b"http://"):
        start = blob.find(scheme)
        while start != -1:
            candidate = _read_url_at(blob, start)
            if candidate and not is_google_news_url(candidate):
                return candidate
            start = blob.find(scheme, start + 1)
    return None


def _read_url_at(blob: bytes, start: int) -> str | None:
    """Extract the URL beginning at `start`, using its length prefix if present.

    Trusting the prefix matters: a greedy scan happily swallows the next
    protobuf field's tag byte and silently corrupts the URL, which then hashes
    to something no other copy of the same article will ever match.
    """
    declared = _varint_before(blob, start)
    raw: bytes | None = None
    if declared is not None and 8 <= declared <= 2048 and start + declared <= len(blob):
        raw = blob[start : start + declared]
        if not _URL_IN_BYTES.fullmatch(raw):
            raw = None
    if raw is None:
        match = _URL_IN_BYTES.match(blob, start)
        if not match:
            return None
        raw = match.group(0)

    try:
        found = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    found = found.rstrip(_URL_TRAILING_JUNK.decode())
    return found or None


def resolve_url(url: str, fetcher=None) -> str:
    """Resolve a Google News link to the publisher URL.

    Order matters: try the offline decode first so a run of 100 items does not
    become 100 extra HTTP requests, then fall back to following redirects, then
    give up and keep the Google URL. Giving up is fine — it still hashes
    consistently, so it dedupes against itself; it just will not dedupe against
    the same story collected from a different query.
    """
    if not is_google_news_url(url):
        return url

    decoded = decode_google_news_url(url)
    if decoded:
        return decoded

    if fetcher is not None:
        try:
            resolved = fetcher.resolve(url)
            if resolved and not is_google_news_url(resolved):
                return resolved
        except Exception as exc:  # noqa: BLE001 - never lose an item over this
            log.warning("redirect resolution failed for %s: %s", url, exc)

    log.info("could not resolve Google News URL, keeping as-is: %s", url)
    return url


def normalise_title(title: str) -> str:
    """Strip the publisher suffix and collapse whitespace before comparing."""
    t = " ".join((title or "").split())
    t = _TITLE_SUFFIX.sub("", t)
    return t.strip().lower()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def window_start(now: datetime | None = None, hours: int = DEDUPE_WINDOW_HOURS) -> str:
    now = now or datetime.now(UTC)
    return (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def find_duplicate_title(
    title: str,
    candidates: list[tuple[int, str, str]],
    *,
    published_at: str | None = None,
    threshold: int = TITLE_SIMILARITY_THRESHOLD,
    window_hours: int = DEDUPE_WINDOW_HOURS,
) -> int | None:
    """Return the id of an already-stored item covering the same story, or None.

    `candidates` is (id, title, timestamp) as returned by db.recent_titles.
    Clustering is limited to a 72h window because the same headline a month
    apart is usually a different award.
    """
    if not title:
        return None
    target = normalise_title(title)
    if not target:
        return None
    target_ts = _parse_ts(published_at)

    best_id: int | None = None
    best_score = float(threshold)
    for cand_id, cand_title, cand_ts_raw in candidates:
        if target_ts is not None:
            cand_ts = _parse_ts(cand_ts_raw)
            if cand_ts is not None and abs((target_ts - cand_ts).total_seconds()) > (
                window_hours * 3600
            ):
                continue
        score = fuzz.token_set_ratio(target, normalise_title(cand_title))
        if score > best_score:
            best_score = score
            best_id = cand_id
    return best_id
