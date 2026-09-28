# websearch usage examples

All commands assume you are in the skill directory and ran
`pip install -r scripts/requirements.txt` once. For brevity the examples
use `websearch` as shorthand for `python3 scripts/websearch.py`.

## 1. Basic web search (compact output)

```bash
websearch search "python asyncio tutorial" --num 5
```

```
status=ok backend=bing_rss results=5
1. Async IO in Python: A Complete Walkthrough
   https://realpython.com/async-io-python/
   Async IO is a concurrent programming design that ...
```

## 2. News search, last day only, as JSON

```bash
websearch news "gempa bumi" --freshness day --num 5 --json | jq '.results[] | {title, url, age}'
```

`--freshness` accepts `hour`, `day`, `week`, `month`.

## 3. Probe a URL before scraping (cheap check)

```bash
websearch probe https://realpython.com/async-io-python/
```

```
url: https://realpython.com/async-io-python/
final_url: https://realpython.com/async-io-python/
ok: yes
status: 200
content_type: text/html
content_length: 48210
is_binary: no
title: Async IO in Python: A Complete Walkthrough
```

Skip the scrape when `is_binary: yes` or `ok: no`.

## 4. Scrape readable article text

```bash
websearch scrape https://realpython.com/async-io-python/ --max-chars 8000
```

```
url: https://realpython.com/async-io-python/
final_url: https://realpython.com/async-io-python/
chars: 8000/23500 truncated=yes next_offset=8000
----
<article text as markdown>
```

## 5. Continue a long page with --offset

```bash
websearch scrape https://realpython.com/async-io-python/ --max-chars 8000 --offset 8000
```

Keep following `next_offset` until `truncated=no`.

## 6. Search, pick a URL, scrape it (typical agent loop)

```bash
websearch search "curl_cffi impersonate" --num 3 --json | jq -r '.results[0].url'
websearch scrape "$(websearch search "curl_cffi impersonate" --num 1 --json | jq -r '.results[0].url')" --max-chars 6000
```

## 7. Handle blocked/empty honestly

```bash
websearch search "obscure topic xyz" --json | jq '{status, backend, message}'
# {"status": "blocked", "backend": "", "message": "Backend returned challenge/captcha: ..."}
```

`status` is `ok`, `empty`, `blocked`, or `error`. A non-`ok` status means
retry later or narrow the query, not that the topic has no sources.

## 8. Pipe compact results into a second tool

```bash
websearch news "AI regulation" --num 10 | grep -E "^[0-9]+\. " | head -5
```

## 9. News via Google News RSS (first backend in the news chain)

```bash
websearch news "gempa bumi" --num 5
```

```
status=ok backend=google_news_rss results=5
1. Gempa Bumi Terkini 3,5 M Guncang Kota Kendari, Tak Berpotensi Tsunami
   https://news.google.com/rss/articles/CBMi0w...
   [source=Databoks Katadata age=Tue, 22 Sep 2026 20:42:27 GMT]
```

Google News RSS is tried first for `news` because it is rarely blocked;
the chain falls back to Bing News HTML, then Bing News RSS.

## 10. Cheapest discovery: --urls-only piped into scrape

```bash
websearch search "trafilatura deduplicate" --num 3 --urls-only | while read -r u; do
  websearch scrape "$u" --max-chars 4000
done
```

`--urls-only` prints one URL per line and nothing else, so no tokens are
spent on titles or snippets when you only need links.
