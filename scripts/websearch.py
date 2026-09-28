#!/usr/bin/env python3
"""websearch: efficient local web search and scrape CLI for coding agents.

Single-file, stdlib + curl_cffi/trafilatura/lxml only. Logic ported from the
proven local-scrape v2 server (MCP plumbing, caches, and batch scraping
removed: each CLI run is one shot, so cross-call caches cannot help).

Commands: search | news | scrape | probe. Default output is compact text for
token efficiency; --json prints the full structured response.
"""
from __future__ import annotations

import argparse
import base64
import html as html_lib
import ipaddress
import json
import random
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from functools import lru_cache
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

from curl_cffi import CurlOpt, requests as cffi_requests
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import IncompleteRead as CurlIncompleteRead
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
from lxml import etree, html as lxml_html
from trafilatura import extract

try:
    from trafilatura.settings import DEFAULT_CONFIG as _TRAF_DEFAULT_CONFIG
except ImportError:  # pragma: no cover - compatibility with older Trafilatura
    _TRAF_DEFAULT_CONFIG = None

__version__ = "1.0.0"


class InputError(Exception):
    """Usage/validation problem (bad URL, query, or flag). Exit code 2."""


class FetchError(Exception):
    """Network, HTTP, or extraction failure. Exit code 1 (or structured status)."""


class _HTTPStatusError(RuntimeError):
    def __init__(self, code: int, retry_after: float | None = None):
        self.status_code = code
        self.retry_after = retry_after
        super().__init__(f"HTTP {code}")


class _TooLarge(RuntimeError):
    pass


class _BinaryContent(RuntimeError):
    def __init__(self, content_type: str):
        self.content_type = content_type
        super().__init__(content_type)


ALLOWED_SCHEMES = {"http", "https"}
SEARCH_TIMEOUT = 12
SCRAPE_TIMEOUT = 20
PROBE_TIMEOUT = 10
MAX_RETRIES = 1
# Exponential backoff + full jitter so concurrent retries do not thunder.
RETRY_BACKOFF = 0.5
# Total wall-clock budget for all attempts incl. jitter/backoff; an attempt
# only gets the remaining time, so a retry never doubles the timeout.
RETRY_TIME_BUDGET = 1.0
MIN_RETRY_REMAINING = 0.25
MAX_REDIRECTS = 5
MAX_RESULTS = 50
MAX_QUERY_CHARS = 1000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONTENT_CHARS = 100_000
DEFAULT_SCRAPE_CHARS = 8_000
MIN_MAX_CHARS = 50
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
TRACKING = {
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid", "vero_id",
    # Bing appends these to article URLs; not part of the real URL.
    "ocid", "cvid", "form", "sp", "ghc", "ref", "referrer",
}
# NAT64 well-known prefix (RFC 6052). Addresses inside it wrap an IPv4: the
# wrapped IPv4 itself must be global, otherwise 64:ff9b::7f00:1 (127.0.0.1)
# and 64:ff9b::a9fe:a9fe (IMDS) would slip through.
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Content that cannot be extracted as text by this tool.
BINARY_CONTENT_TYPES = {
    "application/pdf", "application/zip", "application/gzip", "application/x-gzip",
    "application/x-tar", "application/x-7z-compressed", "application/x-bzip2",
    "application/x-rar-compressed", "application/rtf",
    "application/epub+zip", "application/msword", "application/vnd.ms-",
    "application/vnd.openxmlformats-officedocument",
}
BINARY_CONTENT_PREFIXES = ("image/", "video/", "audio/", "font/")
# application/octet-stream is deliberately NOT listed: servers often send text
# files with that type. For unknown types, magic bytes decide instead.
BINARY_MAGIC = (
    b"%PDF", b"PK\x03\x04", b"\x1f\x8b", b"\x89PNG", b"RIFF", b"\xff\xd8\xff",
    b"7z\xbc\xaf", b"Rar!", b"\x00\x00\x01\x00", b"OggS", b"\x42\x4d",
)
# qft=interval="N" -> results from the last N (Bing web and news both honor it).
FRESHNESS_INTERVAL = {"hour": "4", "day": "7", "week": "8", "month": "9"}
BLOCK_MARKERS = (
    "unusual traffic",
    "automated query",
    "access denied",
    "verify you are human",
    "are you a robot",
)
# DuckDuckGo answers bot challenges with HTTP 202 + anomaly page (not 429).
DDG_CHALLENGE_MARKERS = ("anomaly-modal", "challenge-form", "select all images")
# Title vs snippet weight for relevance re-ranking; Jaccard for near-dup.
RELEVANCE_TITLE_WEIGHT = 3.0
NEARDUP_JACCARD = 0.85
# Small EN+ID stopword list so relevance is not dominated by common words.
_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been by as at from "
    "that this it its into over after before between through during about "
    "di dan yang untuk dari pada ke dengan adalah ini itu atau sebagai dalam "
    "oleh karena akan telah ada juga tidak bisa dapat para sebuah".split()
)
# Probe only reads the head of the body to learn status/type/size/title.
PROBE_MAX_BYTES = 64 * 1024
MAX_TREE_SIZE = 50_000

# One session per thread: curl_options (DNS pinning via CURLOPT_RESOLVE) is
# Session-level state in curl_cffi, so a shared session + pre-GET mutation
# would race between concurrent requests. Thread-local keeps pooling without
# the race.
_TLS = threading.local()


def _get_session():
    sess = getattr(_TLS, "session", None)
    if sess is None:
        sess = cffi_requests.Session(impersonate="chrome", trust_env=False)
        _TLS.session = sess
    return sess


_TRAFILATURA_CONFIG = None
if _TRAF_DEFAULT_CONFIG is not None:
    _TRAFILATURA_CONFIG = deepcopy(_TRAF_DEFAULT_CONFIG)
    _TRAFILATURA_CONFIG.setdefault("DEFAULT", {})["MAX_TREE_SIZE"] = str(MAX_TREE_SIZE)


def _is_binary_content_type(value: str | None) -> bool:
    if not value:
        return False
    ct = value.split(";", 1)[0].strip().lower()
    return ct.startswith(BINARY_CONTENT_PREFIXES) or any(
        ct == known or ct.startswith(known) for known in BINARY_CONTENT_TYPES
    )


def _is_binary_content_type_or_magic(content_type: str | None, raw: bytes) -> bool:
    """Binary detection from content-type, or from magic bytes when the type
    is unknown (e.g. 'application/octet-stream')."""
    if _is_binary_content_type(content_type):
        return True
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in {"", "application/octet-stream", "binary/octet-stream", "application/unknown"}:
        head = raw[:1024]
        return head.startswith(BINARY_MAGIC) or b"\x00" in head
    return False


@lru_cache(maxsize=2048)
def _host_ascii(host: str) -> str:
    raw = host.split("%", 1)[0]
    try:
        ipaddress.ip_address(raw)
        return raw.lower()
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            raise InputError(f"Invalid hostname: {host!r}") from None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable addresses.

    `is_global` follows the IANA Special-Purpose Address Registry, so
    64:ff9b::/96 (NAT64, Globally Reachable=True) is still rejected while
    64:ff9b:1::/48, 100::/64, 2001:db8::/32, 2002::/16, fc00::/7, fe80::/10,
    CGNAT, TEST-NET, and loopback stay blocked.
    """
    if ip.version == 6 and ip in NAT64:
        # De-embed the IPv4 (RFC 6052), then validate the real address too.
        return ipaddress.IPv4Address(ip.packed[12:16]).is_global
    return ip.is_global


def _public_ips(host: str) -> list[str]:
    """The global-IP subset for a host; error only if there is none.

    Dual-stack hosts often also return NAT64/6to4 or other aliases. Such
    addresses must be SKIPPED, not reject the whole host, as long as the host
    has at least one usable global IP.
    """
    host = _host_ascii(host)
    if host == "localhost":
        raise InputError("Target localhost is blocked to prevent SSRF.")

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise InputError(f"DNS failed for host '{host}'.") from None

    out: list[str] = []
    blocked: list[str] = []
    seen: set[str] = set()

    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not _is_public_ip(ip):
            if raw not in blocked:
                blocked.append(raw)
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)

    if not out:
        detail = ", ".join(blocked[:4]) if blocked else "none"
        raise InputError(
            f"Target '{host}' has no usable public IP "
            f"(non-public: {detail})."
        )
    return out


def _validate_url(url: str) -> tuple[str, list[str]]:
    if not isinstance(url, str) or not url.strip():
        raise InputError("URL is required.")

    p = urlparse(url.strip())
    scheme = p.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InputError(f"Scheme '{p.scheme}' is not allowed; only http/https.")
    if not p.hostname:
        raise InputError("Invalid URL: empty hostname.")
    if p.username is not None or p.password is not None:
        raise InputError("URLs with username/password are not supported.")
    try:
        port = p.port
    except ValueError:
        raise InputError("Invalid URL: bad port.") from None

    host = _host_ascii(p.hostname)
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    normalized = urlunparse((scheme, netloc, p.path, "", p.query, ""))
    return normalized, _public_ips(host)


@lru_cache(maxsize=1024)
def _resolve_for(hostname: str, port: int, ips: tuple[str, ...]) -> tuple[str, ...]:
    """CURLOPT_RESOLVE entries; cached because (host, port, ips) repeats."""
    return tuple(f"{hostname}:{port}:{('[' + ip + ']') if ':' in ip else ip}" for ip in ips)


def _retry_after(headers) -> float | None:
    value = headers.get("retry-after") if headers else None
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 5.0))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            return None
        seconds = dt.timestamp() - time.time()
        return max(0.0, min(seconds, 5.0))
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_once(
    url: str, timeout: float, *, _prevalidated: tuple[str, list[str]] | None = None
) -> tuple[bytes, str]:
    # _fetch validates once (1x DNS); reuse it instead of getaddrinfo twice.
    # curl_cffi does not support per-request trust_env/curl_options
    # (Session-level only). The session comes from _get_session() (one per
    # thread), so the curl_options mutation + GET() stays isolated between
    # concurrent requests. trust_env=False is set at Session creation.
    current, ips = _prevalidated if _prevalidated is not None else _validate_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        p = urlparse(current)
        port = p.port or (443 if p.scheme == "https" else 80)
        sess = _get_session()
        sess.curl_options = {CurlOpt.RESOLVE: list(_resolve_for(p.hostname or "", port, tuple(ips)))}

        r = sess.get(
            current,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            if r.status_code in REDIRECT_STATUS:
                location = r.headers.get("location")
                if not location:
                    raise RuntimeError(f"HTTP {r.status_code} without Location header")
                current, ips = _validate_url(urljoin(current, location))
                continue

            if r.status_code >= 400:
                raise _HTTPStatusError(r.status_code, _retry_after(r.headers))

            content_type = r.headers.get("content-type")
            # Reject known binary types before downloading the body. For generic
            # octet-stream, magic-byte detection still runs on the first chunk.
            if _is_binary_content_type(content_type):
                raise _BinaryContent(content_type)

            cl = r.headers.get("content-length")
            try:
                if cl and int(cl) > MAX_RESPONSE_BYTES:
                    raise _TooLarge()
            except ValueError:
                pass

            chunks: list[bytes] = []
            total = 0
            first_chunk = True
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if first_chunk:
                    first_chunk = False
                    if _is_binary_content_type_or_magic(content_type, chunk):
                        raise _BinaryContent(content_type or "application/octet-stream")
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _TooLarge()
                chunks.append(chunk)
            body = b"".join(chunks)

            # Without this guard a PDF/image becomes tens of thousands of
            # mojibake characters that burn token budget for no information.
            if _is_binary_content_type_or_magic(content_type, body):
                raise _BinaryContent(content_type or "application/octet-stream")
            return body, current
        finally:
            try:
                r.close()
            except Exception:
                pass

    raise RuntimeError(f"Too many redirects (>{MAX_REDIRECTS}).")


def _fetch(
    url: str,
    *,
    timeout: int,
    retries: int = MAX_RETRIES,
    prevalidated: tuple[str, list[str]] | None = None,
) -> tuple[bytes, str]:
    prevalidated = prevalidated or _validate_url(url)
    last = None
    deadline = time.monotonic() + float(timeout) * RETRY_TIME_BUDGET
    attempts = 0

    for attempt in range(retries + 1):
        retry_after = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts = attempt + 1
        try:
            # The per-attempt timeout is capped by the remaining total retry
            # budget, so a slow first attempt is not followed by a second full
            # timeout.
            attempt_timeout = min(float(timeout), remaining)
            return _fetch_once(
                url,
                attempt_timeout,
                _prevalidated=prevalidated,
            )
        except InputError:
            raise
        except _TooLarge as exc:
            raise FetchError(f"Response too large; limit is {MAX_RESPONSE_BYTES} bytes.") from exc
        except _BinaryContent as exc:
            raise FetchError(
                f"Content {exc.content_type} cannot be extracted as text by this tool."
            ) from exc
        except _HTTPStatusError as exc:
            last = exc
            if exc.status_code not in RETRYABLE_STATUS or attempt >= retries:
                raise FetchError(f"Failed to fetch {url}: HTTP {exc.status_code}.") from exc
            delay = exc.retry_after if exc.retry_after is not None else RETRY_BACKOFF * (2 ** attempt)
            retry_after = exc.retry_after
        except (CurlConnectionError, CurlTimeout, CurlIncompleteRead) as exc:
            last = exc
            if attempt >= retries:
                break
            delay = RETRY_BACKOFF * (2 ** attempt)
        except Exception as exc:
            # Programming/parse/validation errors should not be retried blindly.
            raise FetchError(f"Failed to fetch {url}: {exc}") from exc

        remaining = deadline - time.monotonic()
        if remaining <= MIN_RETRY_REMAINING:
            break
        if delay >= remaining:
            break
        pause = (
            min(delay, remaining - MIN_RETRY_REMAINING)
            if retry_after is not None
            else min(random.uniform(0.0, delay), remaining - MIN_RETRY_REMAINING)
        )
        if pause > 0:
            time.sleep(pause)

    raise FetchError(f"Failed to fetch {url} after {attempts} attempts: {last}")


# ---------------------------------------------------------------------------
# URL canonicalization and result cleaning
# ---------------------------------------------------------------------------

@lru_cache(maxsize=2048)
def _bing_redirect(url: str) -> str:
    try:
        p = urlparse(url)
        host = _host_ascii(p.hostname or "")
        if not (host == "bing.com" or host.endswith(".bing.com")) or "/ck/a" not in p.path.lower():
            return url
        encoded = next(
            (v for k, v in parse_qsl(p.query, keep_blank_values=True) if k == "u"),
            "",
        )
        if not encoded.startswith("a1"):
            return url
        raw = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
        return decoded if decoded.startswith(("http://", "https://")) else url
    except (ValueError, UnicodeError):
        return url


@lru_cache(maxsize=2048)
def _canonical_url(url: str) -> str:
    p = urlparse(_bing_redirect(url.strip()))
    if p.scheme.lower() not in ALLOWED_SCHEMES or not p.hostname:
        return url
    scheme, host = p.scheme.lower(), _host_ascii(p.hostname)
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc += f":{port}"
    query = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if not kl.startswith("utm_") and kl not in TRACKING:
            query.append((k, v))
    return urlunparse((scheme, netloc, p.path or "/", "", urlencode(query, doseq=True), ""))


def _usable_result_url(url: str) -> str | None:
    canonical = _canonical_url(url)
    p = urlparse(canonical)
    if p.scheme.lower() not in ALLOWED_SCHEMES or not p.hostname:
        return None
    host = _host_ascii(p.hostname)
    if host == "bing.com" or host.endswith(".bing.com"):
        return None
    return canonical


def _dedupe(results: list[dict], count: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in results:
        href = raw.get("url", "")
        if not isinstance(href, str) or not href:
            continue
        url = _usable_result_url(href)
        if not url or url in seen:
            continue
        seen.add(url)
        item = dict(raw)
        item["url"] = url
        out.append(item)
        if len(out) >= count:
            break
    return out


# Pre-compiled XPath: avoids compile cost per request (lxml.etree.XPath reuse).
_XP_NEWS_CARDS = etree.XPath('//*[contains(@class,"newsitem")]')
_XP_NEWS_CARDS_FB = etree.XPath('//div[contains(@class,"news-card")]')
_XP_NEWS_TITLE_FB = etree.XPath('.//*[contains(@class,"newscard-title") or contains(@class,"title")][1]')
_XP_A_HREF = etree.XPath('.//a[@href][1]')
_XP_SNIPPET = etree.XPath('.//*[contains(@class,"snippet")][1]')
_XP_NEWS_AGE = etree.XPath('.//div[contains(@class,"source")]//span[@tabindex="0"][1]')
_XP_NEWS_IMG = etree.XPath('.//img[@data-src-hq][1]')
_XP_WEB_ITEMS = etree.XPath('//li[contains(concat(" ", normalize-space(@class), " "), " b_algo ")]')
_XP_WEB_FB = etree.XPath('//h2/a[@href]')
_XP_H2_A = etree.XPath('.//h2/a[@href][1]')
_XP_CAPTION_P = etree.XPath('.//div[contains(@class,"b_caption")]//p[1]')
_XP_DDG_A = etree.XPath('//a[contains(@class,"result__a")]')
_XP_DDG_SNIP = etree.XPath('ancestor::div[contains(@class,"result__body")]//a[contains(@class,"result__snippet")]')


def _text(el) -> str:
    if isinstance(el, list):
        el = el[0] if el else None
    return " ".join(el.itertext()).strip() if el is not None else ""


def _blocked(html: str) -> bool:
    x = html.lower()
    if any(marker in x for marker in BLOCK_MARKERS):
        return True
    return "captcha" in x and any(term in x for term in ("challenge", "verify", "robot", "security"))


def _parse_news(html: str, count: int) -> list[dict]:
    if not html.strip():
        return []
    try:
        tree = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return []

    cards = _XP_NEWS_CARDS(tree)
    if not cards:
        cards = _XP_NEWS_CARDS_FB(tree)

    out: list[dict] = []
    for card in cards:
        title = (card.get("data-title") or card.get("title") or "").strip()
        if not title:
            title = _text(_XP_NEWS_TITLE_FB(card))

        href = (card.get("url") or card.get("data-url") or "").strip()
        if not href:
            a = _XP_A_HREF(card)
            href = a[0].get("href", "") if a else ""
        if not title or not href:
            continue

        href = urljoin("https://www.bing.com/", href)
        source = (card.get("data-author") or card.get("author") or "").strip()
        if not source:
            try:
                source = _host_ascii(urlparse(_canonical_url(href)).hostname or "")
            except InputError:
                source = ""

        # Bing only shows relative time in the aria-label of the span with
        # tabindex="0" (e.g. "6 hours ago"); the visible text is just "6h".
        age_el = _XP_NEWS_AGE(card)
        age = ""
        if age_el:
            age = (age_el[0].get("aria-label") or _text(age_el)).strip()
        img = _XP_NEWS_IMG(card)
        image = urljoin("https://www.bing.com/", img[0].get("data-src-hq", "")) if img else ""

        out.append({
            "title": title,
            "url": href,
            "snippet": _text(_XP_SNIPPET(card)),
            "source": source,
            "age": age,
            "image": image,
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _parse_web(html: str, count: int) -> list[dict]:
    if not html.strip():
        return []
    try:
        tree = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return []

    items = _XP_WEB_ITEMS(tree)
    if not items:
        # Small compatibility fallback for markup changes.
        items = _XP_WEB_FB(tree)

    out: list[dict] = []
    for item in items:
        # Bing marks ad units with class b_ad on the <li>. Organic items are
        # always exactly "b_algo", never "b_ad", so this guard is safe.
        if "b_ad" in item.get("class", "").split():
            continue
        if item.tag == "a":
            a = item
            title = _text(a)
            href = a.get("href", "")
            snippet = ""
        else:
            a = _XP_H2_A(item)
            if not a:
                continue
            a = a[0]
            title = _text(a)
            href = a.get("href", "")
            snippet = _text(_XP_CAPTION_P(item))

        if not title or not href:
            continue
        out.append({
            "title": title,
            "url": urljoin("https://www.bing.com/", href),
            "snippet": snippet,
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _unwrap_apiclick(url: str) -> str:
    """Bing News RSS wraps links in /news/apiclick.aspx?url=<encoded target>."""
    try:
        p = urlparse(html_lib.unescape(url))
    except ValueError:
        return url
    if "apiclick.aspx" not in p.path.lower():
        return url
    target = next(
        (v for k, v in parse_qsl(p.query, keep_blank_values=True) if k == "url"),
        "",
    )
    return target if target.startswith(("http://", "https://")) else url


def _parse_bing_rss(raw: bytes, count: int) -> list[dict]:
    try:
        xml = raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, ValueError):
        return []
    out: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        item = m.group(1)

        def tag(name: str) -> str:
            mm = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
            return html_lib.unescape(mm.group(1).strip()) if mm else ""

        title, link = tag("title"), tag("link")
        if not title or not link:
            continue
        snippet = re.sub(r"<[^>]+>", "", tag("description")).strip()
        out.append({
            "title": title,
            "url": _unwrap_apiclick(link),
            "snippet": snippet,
            "age": tag("pubDate"),
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _parse_google_news_rss(raw: bytes, count: int) -> list[dict]:
    # Keep the news.google.com article URL as-is: live-checked 2026-09-28 it
    # only 302s to a locale-tagged copy of itself and renders the publisher
    # page via JS, so the skill's fetcher lands on Google's JS shell. No
    # server-side publisher URL is exposed; the CBM article id is not decoded.
    # Snippet is empty on purpose: <description> only repeats title + source,
    # so including it would double token cost for zero information.
    try:
        xml = raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, ValueError):
        return []
    out: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        item = m.group(1)

        def tag(name: str) -> str:
            mm = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.S)
            return html_lib.unescape(mm.group(1).strip()) if mm else ""

        title, link = tag("title"), tag("link")
        if not title or not link:
            continue
        out.append({
            "title": title,
            "url": link,
            "snippet": "",
            "source": tag("source"),
            "age": tag("pubDate"),
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _unwrap_ddg(href: str) -> str | None:
    """DDG wraps results in //duckduckgo.com/l/?uddg=<encoded>; drop ad wrappers."""
    if not href:
        return None
    try:
        p = urlparse(urljoin("https://duckduckgo.com", href))
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if "duckduckgo.com" in host:
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if "ad_provider" in q or "ad_domain" in q:
            return None
        uddg = q.get("uddg", "")
        return uddg if uddg.startswith(("http://", "https://")) else None
    return href if p.scheme in ("http", "https") else None


def _parse_ddg(html: str, count: int) -> list[dict]:
    if not html.strip():
        return []
    try:
        tree = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return []
    out: list[dict] = []
    for a in _XP_DDG_A(tree):
        title = _text(a)
        href = _unwrap_ddg(a.get("href", ""))
        if not title or not href:
            continue
        out.append({
            "title": title,
            "url": href,
            "snippet": _text(_XP_DDG_SNIP(a)),
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _parse_marginalia(raw: bytes, count: int) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return []
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title, url = str(it.get("title", "")), str(it.get("url", ""))
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "snippet": str(it.get("description", "")),
        })
        if len(out) >= count:
            break
    return _dedupe(out, count)


def _backend_blocked(name: str, raw: bytes) -> bool:
    """True if the backend answered with a challenge/captcha instead of results."""
    text = raw[:8 * 1024].decode("utf-8", "replace").lower()
    if name in ("bing_html", "bing_news_html"):
        return _blocked(text)
    if name in ("bing_rss", "bing_news_rss", "google_news_rss"):
        head = raw.lstrip()[:5].lower()
        # Real RSS starts with <?xml or <rss; anything else is a block page.
        return not (head.startswith(b"<?xml") or head.startswith(b"<rss"))
    if name == "duckduckgo":
        return _blocked(text) or any(m in text for m in DDG_CHALLENGE_MARKERS)
    return False  # marginalia: JSON API; failures surface as HTTP errors


# ---------------------------------------------------------------------------
# Relevance re-ranking and near-deduplication
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def _relevance_score(query: str, title: str, snippet: str) -> float:
    """Lightweight relevance: query-term coverage, title weighted 3x.

    Re-sorts the backend's raw order by topical match; cheap enough to run on
    every search (no model, no network). Exposed as `score` so the agent can
    judge too.
    """
    qterms = set(_tokenize(query))
    if not qterms:
        return 0.0
    tt, st = _tokenize(title), _tokenize(snippet)
    if not tt and not st:
        return 0.0
    hit, matched = 0.0, 0
    for term in qterms:
        th, sh = tt.count(term), st.count(term)
        if th or sh:
            matched += 1
            hit += RELEVANCE_TITLE_WEIGHT * th + sh
    coverage = matched / len(qterms)
    return hit * (0.5 + 0.5 * coverage) / (1.0 + 0.05 * len(tt))


def _near_dedupe(results: list[dict]) -> list[dict]:
    """Drop results whose title+snippet token set ~duplicates an already kept one."""
    out: list[dict] = []
    seen: list[set[str]] = []
    for r in results:
        toks = set(_tokenize(f"{r.get('title', '')} {r.get('snippet', '')}"))
        if not toks:
            out.append(r)
            continue
        if any(len(toks & s) / len(toks | s) >= NEARDUP_JACCARD for s in seen):
            continue
        seen.append(toks)
        out.append(r)
    return out


def _rerank(query: str, results: list[dict]) -> list[tuple[float, dict]]:
    scored = [
        (_relevance_score(query, str(r.get("title", "")), str(r.get("snippet", ""))), r)
        for r in results
    ]
    scored.sort(key=lambda p: p[0], reverse=True)  # stable: ties keep backend order
    return scored


# ---------------------------------------------------------------------------
# Multi-backend search: chain of engines, first usable result wins.
# ---------------------------------------------------------------------------

def _search_url(query: str, count: int, news: bool, freshness: str = "") -> str:
    params = {"q": query, "count": str(count)}
    if news:
        # Default news: newest first, not "best match".
        params["qft"] = f'interval="{FRESHNESS_INTERVAL[freshness]}"' if freshness else 'sortbydate="1"'
        params["form"] = "YFNR"
    elif freshness:
        # Bing web also honors qft=interval; v2 only exposed it for news.
        params["qft"] = f'interval="{FRESHNESS_INTERVAL[freshness]}"'
    return "https://www.bing.com/" + ("news/search?" if news else "search?") + urlencode(params)


def _bing_rss_url(query: str, count: int, news: bool) -> str:
    """Bing machine format: clean XML, direct URLs, rarely challenged."""
    params = {"q": query, "format": "rss", "count": str(count)}
    base = "https://www.bing.com/news/search?" if news else "https://www.bing.com/search?"
    return base + urlencode(params)


def _ddg_url(query: str, count: int) -> str:
    """DuckDuckGo no-JS HTML endpoint."""
    return "https://html.duckduckgo.com/html/?" + urlencode({"q": query})


def _marginalia_url(query: str, count: int) -> str:
    """Marginalia public JSON API: keyless, documented, niche independent index."""
    return "https://api.marginalia.nu/public/search/" + quote(query) + f"?count={min(count, 20)}"


def _google_news_rss_url(query: str, count: int) -> str:
    # Live-checked 2026-09-28: hl=id&gl=ID&ceid=ID:id returns 90+ relevant
    # items for both an Indonesian ("gempa bumi hari ini") and an English
    # ("tesla stock") query; omitting the locale params drops to ~38 items.
    params = {"q": query, "hl": "id", "gl": "ID", "ceid": "ID:id"}
    return "https://news.google.com/rss/search?" + urlencode(params)


def _search_backends(
    query: str,
    count: int,
    news: bool,
    freshness: str = "",
) -> list[tuple[str, str, Callable, bool]]:
    """(backend_name, url, parser, raw_is_bytes) in priority order."""
    if news:
        return [
            ("google_news_rss", _google_news_rss_url(query, count), _parse_google_news_rss, True),
            ("bing_news_html", _search_url(query, count, True, freshness), _parse_news, False),
            ("bing_news_rss", _bing_rss_url(query, count, True), _parse_bing_rss, True),
        ]
    return [
        ("bing_html", _search_url(query, count, False, freshness), _parse_web, False),
        ("bing_rss", _bing_rss_url(query, count, False), _parse_bing_rss, True),
        ("duckduckgo", _ddg_url(query, count), _parse_ddg, False),
        ("marginalia", _marginalia_url(query, count), _parse_marginalia, True),
    ]


def _shape_results(
    ranked: list[tuple[float, dict]],
    source_type: str,
    backend: str,
) -> list[dict]:
    """Result: {title, url, snippet, backend, score} (+ source/age for news)."""
    out: list[dict] = []
    for score, raw in ranked:
        item: dict = {
            "title": str(raw.get("title", "")),
            "url": str(raw.get("url", "")),
            "snippet": str(raw.get("snippet", "")),
            "backend": backend,
            "score": round(score, 3),
        }
        if source_type == "news":
            if raw.get("source"):
                item["source"] = str(raw["source"])
            if raw.get("age"):
                item["age"] = str(raw["age"])
        out.append(item)
    return out


def _search_impl(
    query: str,
    count: int,
    *,
    news: bool,
    freshness: str = "",
) -> dict:
    """Try backends in order; first backend with usable results wins.

    Never raises for backend problems: they become structured status
    (blocked/error/empty) so the agent can decide what to do next.
    SearchResponse: {query, source_type, status, backend, message, results}.
    """
    source_type = "news" if news else "web"
    blocked_any = False
    errors: list[str] = []
    for name, url, parse, is_bytes in _search_backends(query, count, news, freshness):
        try:
            raw, _ = _fetch(url, timeout=SEARCH_TIMEOUT)
        except (InputError, FetchError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if _backend_blocked(name, raw):
            blocked_any = True
            errors.append(f"{name}: challenge/captcha")
            continue
        try:
            results = parse(raw if is_bytes else raw.decode("utf-8", "replace"), count)
        except Exception as exc:  # a parser must never kill the chain
            errors.append(f"{name}: parse error ({exc})")
            continue
        results = _near_dedupe(results)
        if not results:
            continue
        ranked = _rerank(query, results)
        # Relevance guard: some Bing RSS answers on some IPs return "decoy
        # items" (valid results unrelated to the query). If no query term
        # appears in any title/snippet, this backend is not usable -> try next.
        if set(_tokenize(query)) and ranked[0][0] <= 0:
            errors.append(f"{name}: results not relevant to query")
            continue
        return {
            "query": query,
            "source_type": source_type,
            "status": "ok",
            "backend": name,
            "message": "",
            "results": _shape_results(ranked, source_type, name),
        }
    if blocked_any:
        status, message = "blocked", "Backend returned challenge/captcha: " + "; ".join(errors[:3])
    elif errors:
        status, message = "error", "; ".join(errors[:3])
    else:
        status, message = "empty", "All backends returned 0 results for this query."
    return {
        "query": query,
        "source_type": source_type,
        "status": status,
        "backend": "",
        "message": message,
        "results": [],
    }


# ---------------------------------------------------------------------------
# Input validators (InputError -> exit code 2)
# ---------------------------------------------------------------------------

def _query(value: str) -> str:
    if not isinstance(value, str):
        raise InputError("query must be a string.")
    value = value.strip()
    if not value:
        raise InputError("query must not be empty.")
    if len(value) > MAX_QUERY_CHARS:
        raise InputError(f"query is limited to {MAX_QUERY_CHARS} characters.")
    return value


def _count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InputError("count must be an integer >= 1.")
    return min(MAX_RESULTS, value)


def _max_chars(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < MIN_MAX_CHARS:
        raise InputError(f"max_chars must be an integer >= {MIN_MAX_CHARS}.")
    return min(MAX_CONTENT_CHARS, value)


def _char_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError("char_offset must be an integer >= 0.")
    return value


def _freshness(value: str) -> str:
    if not isinstance(value, str):
        raise InputError("freshness must be a string.")
    value = value.strip().lower()
    if not value:
        return ""
    if value not in FRESHNESS_INTERVAL:
        raise InputError("freshness must be one of: hour, day, week, month.")
    return value


def _output_format(value: str) -> str:
    if not isinstance(value, str):
        raise InputError("format must be a string.")
    value = value.strip().lower()
    if value not in {"markdown", "txt", "xml", "json"}:
        raise InputError("format must be markdown, txt, xml, or json.")
    return value


# ---------------------------------------------------------------------------
# Scrape: fetch -> extract -> paginate
# ---------------------------------------------------------------------------

def _dump_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _truncate_json(value: str, limit: int = MAX_CONTENT_CHARS) -> str:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        data, text = {"truncated": True, "text": ""}, value
    else:
        out = _dump_json(data)
        if len(out) <= limit:
            return out
        text = data.get("text", "") if isinstance(data, dict) and isinstance(data.get("text"), str) else ""
        data = {k: data[k] for k in ("title", "url", "date", "author") if isinstance(data, dict) and k in data}
        data.update(truncated=True, text=text)

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data["text"] = text[:mid]
        if len(_dump_json(data)) <= limit:
            lo = mid
        else:
            hi = mid - 1
    data["text"] = text[:lo]
    return _dump_json(data)


def _truncate_xml(value: str, limit: int = MAX_CONTENT_CHARS) -> str:
    try:
        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
        root = etree.fromstring(value.encode("utf-8", "replace"), parser=parser)
        normal = etree.tostring(root, encoding="unicode")
        if len(normal) <= limit:
            return normal
    except (etree.XMLSyntaxError, UnicodeError):
        pass

    wrapper = etree.Element("document")
    text_node = etree.SubElement(wrapper, "text")
    lo, hi = 0, min(len(value), limit)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        text_node.text = value[:mid]
        if len(etree.tostring(wrapper, encoding="unicode")) <= limit:
            lo = mid
        else:
            hi = mid - 1
    text_node.text = value[:lo]
    return etree.tostring(wrapper, encoding="unicode")


def _as_json_text(raw: bytes) -> str | None:
    """JSON API bypass: when the body is valid JSON, pretty-print it directly
    instead of running trafilatura (which is not built for JSON)."""
    stripped = raw.strip()
    if stripped[:1] not in (b"{", b"["):
        return None
    try:
        return json.dumps(
            json.loads(stripped.decode("utf-8", "replace")),
            ensure_ascii=False,
            indent=2,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _extract_with_trafilatura(raw: bytes, final_url: str, output_format: str) -> str:
    # deduplicate must be False: any truthy value (even an LRUCache instance)
    # enables GLOBAL cross-call dedup, so re-scraping the same URL in one
    # process returns empty ("discarding data"). False = deterministic.
    kwargs = dict(
        url=final_url,
        output_format=output_format,
        include_links=True,
        include_formatting=output_format != "json",
        include_comments=False,
        include_tables=True,
        deduplicate=False,
        with_metadata=(output_format == "json"),
        date_extraction_params={"extensive_search": True} if output_format == "json" else None,
    )
    if _TRAFILATURA_CONFIG is not None:
        kwargs["config"] = _TRAFILATURA_CONFIG

    try:
        result = extract(raw, **kwargs)
    except (TypeError, ValueError):
        result = None

    # Fast is the normal hot path. Escalate only when nothing useful came out.
    if not result:
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("fast", None)
        try:
            result = extract(raw, **fallback_kwargs)
        except (TypeError, ValueError):
            result = None

    if not result:
        raise FetchError(
            "Fetched the page but main extraction failed. "
            "Try another URL."
        )
    return result


def _paginate(full: str, offset: int, limit: int) -> tuple[str, bool, int | None]:
    """Pure helper: slice one page; returns (page, truncated, next_offset)."""
    page = full[offset:offset + limit]
    truncated = offset + limit < len(full)
    return page, truncated, (offset + limit if truncated else None)


def scrape_url_impl(
    url: str,
    *,
    output_format: str = "markdown",
    max_chars: int = DEFAULT_SCRAPE_CHARS,
    char_offset: int = 0,
    timeout: int = SCRAPE_TIMEOUT,
) -> dict:
    """Scrape one URL -> structured text with char_offset pagination.

    Returns {url, final_url, content, content_chars, content_total_chars,
    truncated, next_char_offset, error}. Raises InputError (bad input) or
    FetchError (fetch/extract failure).
    """
    output_format = _output_format(output_format)
    limit = _max_chars(max_chars)
    offset = _char_offset(char_offset)
    original, ips = _validate_url(url)

    try:
        raw, final_url = _fetch(original, timeout=timeout, prevalidated=(original, ips))
    except InputError as exc:
        # Input was fine; a redirect target failed validation -> fetch problem.
        raise FetchError(str(exc)) from exc

    full = _as_json_text(raw)
    if full is None:
        full = _extract_with_trafilatura(raw, final_url, output_format)
    # Keep structural validity for json/xml before paging: re-joined pages
    # still form a valid document.
    if output_format == "json":
        full = _truncate_json(full, MAX_CONTENT_CHARS)
    elif output_format == "xml":
        full = _truncate_xml(full, MAX_CONTENT_CHARS)

    page, truncated, next_offset = _paginate(full, offset, limit)
    return {
        "url": original,
        "final_url": final_url,
        "content": page,
        "content_chars": len(page),
        "content_total_chars": len(full),
        "truncated": truncated,
        "next_char_offset": next_offset,
        "error": "",
    }


# ---------------------------------------------------------------------------
# Probe: cheap pre-scrape check (status, type, size, title)
# ---------------------------------------------------------------------------

def _probe_error(url: str, final_url: str, message: str) -> dict:
    return {
        "url": url,
        "final_url": final_url,
        "ok": False,
        "status_code": 0,
        "content_type": "",
        "content_length": None,
        "is_binary": False,
        "title": "",
        "error": message,
    }


def probe_url_impl(url: str, timeout: int = PROBE_TIMEOUT) -> dict:
    """Cheap check before scraping: downloads only the head of the body to
    learn status, content type, size, and title. Does not extract content.

    Returns {url, final_url, ok, status_code, content_type, content_length,
    is_binary, title, error}. Raises InputError for bad input; runtime
    problems become ok=False (never an exception).
    """
    original, ips = _validate_url(url)
    current, cur_ips = original, ips
    for _ in range(MAX_REDIRECTS + 1):
        p = urlparse(current)
        port = p.port or (443 if p.scheme == "https" else 80)
        sess = _get_session()
        sess.curl_options = {CurlOpt.RESOLVE: list(_resolve_for(p.hostname or "", port, tuple(cur_ips)))}
        try:
            r = sess.get(current, timeout=timeout, allow_redirects=False, stream=True)
        except (CurlConnectionError, CurlTimeout) as exc:
            return _probe_error(original, current, f"Connection failed: {exc}")
        try:
            if r.status_code in REDIRECT_STATUS:
                location = r.headers.get("location")
                if not location:
                    return _probe_error(original, current, f"HTTP {r.status_code} without Location header.")
                try:
                    current, cur_ips = _validate_url(urljoin(current, location))
                except InputError as exc:
                    return _probe_error(original, current, str(exc))
                continue
            status_code = r.status_code
            if status_code >= 400:
                out = _probe_error(original, current, f"HTTP {status_code}.")
                out["status_code"] = status_code
                return out
            content_type = r.headers.get("content-type", "")
            cl = r.headers.get("content-length")
            try:
                content_length = int(cl) if cl else None
            except ValueError:
                content_length = None
            head = b""
            try:
                for chunk in r.iter_content(chunk_size=32 * 1024):
                    if chunk:
                        head = chunk
                        break
            except (CurlConnectionError, CurlTimeout, CurlIncompleteRead):
                pass
            is_binary = _is_binary_content_type_or_magic(content_type, head)
            title = ""
            if not is_binary and b"<html" in head[:4096].lower():
                try:
                    doc = lxml_html.fromstring(head.decode("utf-8", "replace"))
                    t = doc.find(".//title")
                    title = (t.text_content().strip() if t is not None else "")[:200]
                except (etree.ParserError, ValueError):
                    pass
            return {
                "url": original,
                "final_url": current,
                "ok": True,
                "status_code": status_code,
                "content_type": content_type.split(";", 1)[0].strip().lower(),
                "content_length": content_length,
                "is_binary": is_binary,
                "title": title,
                "error": "",
            }
        finally:
            try:
                r.close()
            except Exception:
                pass
    return _probe_error(original, current, f"Too many redirects (>{MAX_REDIRECTS}).")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _one_line(text: str, limit: int = 300) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _print_search(resp: dict, as_json: bool, urls_only: bool = False) -> None:
    if as_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return
    if urls_only:
        # Cheapest discovery flow: one URL per line, nothing else.
        for r in resp["results"]:
            print(r["url"])
        return
    status, backend = resp["status"], resp["backend"] or "-"
    n = len(resp["results"])
    if resp["message"]:
        print(f"status={status} backend={backend} message={resp['message']}")
    else:
        print(f"status={status} backend={backend} results={n}")
    for i, r in enumerate(resp["results"], 1):
        print(f"{i}. {r['title']}")
        print(f"   {r['url']}")
        if r.get("snippet"):
            print(f"   {_one_line(r['snippet'])}")
        if r.get("source") or r.get("age"):
            extra = " ".join(
                f"{k}={r[k]}" for k in ("source", "age") if r.get(k)
            )
            print(f"   [{extra}]")


def _print_scrape(resp: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return
    nxt = f" next_offset={resp['next_char_offset']}" if resp["truncated"] else ""
    print(f"url: {resp['url']}")
    print(f"final_url: {resp['final_url']}")
    print(f"chars: {resp['content_chars']}/{resp['content_total_chars']} truncated={'yes' if resp['truncated'] else 'no'}{nxt}")
    print("----")
    print(resp["content"])


def _print_probe(resp: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return
    print(f"url: {resp['url']}")
    print(f"final_url: {resp['final_url']}")
    print(f"ok: {'yes' if resp['ok'] else 'no'}")
    if resp["ok"]:
        print(f"status: {resp['status_code']}")
        print(f"content_type: {resp['content_type'] or '-'}")
        print(f"content_length: {resp['content_length'] if resp['content_length'] is not None else '-'}")
        print(f"is_binary: {'yes' if resp['is_binary'] else 'no'}")
        print(f"title: {resp['title'] or '-'}")
    if resp["error"]:
        print(f"error: {resp['error']}")


def _positive_int(name: str, minimum: int, maximum: int):
    def conv(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{name} must be an integer.")
        if value < minimum or value > maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}."
            )
        return value
    return conv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="websearch",
        description="Efficient local web search and scrape CLI for coding agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="Search the web (multi-backend chain).")
    p.add_argument("query", nargs="+", help="Search query (words are joined with spaces).")
    p.add_argument("--num", type=_positive_int("num", 1, MAX_RESULTS), default=5,
                   help=f"Max results (default 5, max {MAX_RESULTS}).")
    p.add_argument("--freshness", choices=sorted(FRESHNESS_INTERVAL), default=None,
                   help="Limit to recent results: hour, day, week, month.")
    p.add_argument("--json", action="store_true", help="Print the full structured response as JSON.")
    p.add_argument("--urls-only", action="store_true",
                   help="Print one URL per line, nothing else (cheapest discovery).")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("news", help="Search news (newest first).")
    p.add_argument("query", nargs="+", help="Search query (words are joined with spaces).")
    p.add_argument("--num", type=_positive_int("num", 1, MAX_RESULTS), default=5,
                   help=f"Max results (default 5, max {MAX_RESULTS}).")
    p.add_argument("--freshness", choices=sorted(FRESHNESS_INTERVAL), default=None,
                   help="Limit to recent results: hour, day, week, month.")
    p.add_argument("--json", action="store_true", help="Print the full structured response as JSON.")
    p.add_argument("--urls-only", action="store_true",
                   help="Print one URL per line, nothing else (cheapest discovery).")
    p.set_defaults(func=_cmd_news)

    p = sub.add_parser("scrape", help="Extract readable text from one URL.")
    p.add_argument("url", help="URL to scrape.")
    p.add_argument("--max-chars", type=_positive_int("max-chars", MIN_MAX_CHARS, MAX_CONTENT_CHARS),
                   default=DEFAULT_SCRAPE_CHARS,
                   help=f"Max chars per page (default {DEFAULT_SCRAPE_CHARS}).")
    p.add_argument("--offset", type=_positive_int("offset", 0, MAX_CONTENT_CHARS), default=0,
                   help="Character offset to continue a long page (see next_offset).")
    p.add_argument("--format", choices=["markdown", "txt", "xml", "json"], default="markdown",
                   help="Output format (default markdown).")
    p.add_argument("--json", action="store_true",
                   help="Wrap the result as JSON instead of printing raw content.")
    p.set_defaults(func=_cmd_scrape)

    p = sub.add_parser("probe", help="Cheap pre-scrape check: status, type, size, title.")
    p.add_argument("url", help="URL to probe.")
    p.add_argument("--json", action="store_true", help="Print the full structured response as JSON.")
    p.set_defaults(func=_cmd_probe)

    return parser


def _cmd_search(args: argparse.Namespace) -> int:
    resp = _search_impl(_query(" ".join(args.query)), _count(args.num),
                        news=False, freshness=_freshness(args.freshness or ""))
    _print_search(resp, args.json, urls_only=args.urls_only)
    return 0  # empty/blocked/error are structured statuses, not failures


def _cmd_news(args: argparse.Namespace) -> int:
    resp = _search_impl(_query(" ".join(args.query)), _count(args.num),
                        news=True, freshness=_freshness(args.freshness or ""))
    _print_search(resp, args.json, urls_only=args.urls_only)
    return 0


def _cmd_scrape(args: argparse.Namespace) -> int:
    try:
        resp = scrape_url_impl(
            args.url,
            output_format=args.format,
            max_chars=args.max_chars,
            char_offset=args.offset,
        )
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FetchError as exc:
        if args.json:
            print(json.dumps({"url": args.url, "error": str(exc)},
                             ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_scrape(resp, args.json)
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    try:
        resp = probe_url_impl(args.url)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_probe(resp, args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FetchError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except Exception as exc:  # unexpected: never leak a traceback to an agent
        print(f"error: unexpected failure ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
