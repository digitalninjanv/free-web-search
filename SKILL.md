---
name: web-search
description: "Local web search and page scraping CLI for research tasks: search the web or news across multiple backends, probe a URL before scraping, and extract readable article text. Use when you need up-to-date information from the web and want results as compact text or structured JSON."
---

# web-search

Single-file CLI (`scripts/websearch.py`, Python 3.10+, stdlib + curl_cffi/trafilatura/lxml). No API keys. Anti-block via TLS-impersonated HTTP sessions; multi-backend chain so one blocked engine does not kill the search.

## Install

```bash
pip install -r scripts/requirements.txt
```

## When to use

- You need current information from the web (docs, news, prices, facts) during a coding/research task.
- Prefer `probe` before `scrape` on an unfamiliar URL: it is cheap and tells you status, content type, size, and title.
- `search`/`news` return discovery metadata, not evidence. Pick authoritative URLs from the results, then `scrape` them. Cross-check before answering.

## Commands

```bash
python3 scripts/websearch.py search QUERY [--num N] [--freshness hour|day|week|month] [--json] [--urls-only]
python3 scripts/websearch.py news QUERY [--num N] [--freshness hour|day|week|month] [--json] [--urls-only]
python3 scripts/websearch.py scrape URL [--max-chars N] [--offset N] [--format markdown|txt|xml|json] [--json]
python3 scripts/websearch.py probe URL [--json]
```

Backends: web tries Bing HTML, Bing RSS, DuckDuckGo, Marginalia in order.
News tries Google News RSS first (rarely blocked), then Bing News HTML,
then Bing News RSS. First backend with usable results wins.

## Output contract

- Default output is compact text for token efficiency: search/news print one numbered block per result (`title`, `url`, one-line snippet trimmed to ~300 chars); scrape prints a small header then the content.
- `--urls-only` prints one URL per line and nothing else: the cheapest discovery-then-scrape flow.
- `--json` prints the full structured response instead.
- Search response: `{query, source_type, status, backend, message, results}`. Result: `{title, url, snippet, backend, score}` plus `source`/`age` for news.
- `status` is `ok`, `empty`, `blocked`, or `error`. `blocked`/`error`/`empty` are normal outcomes, not failures: narrow the query or retry later.
- `scrape` paginates long pages: if `truncated` is true, call again with `--offset <next_char_offset>`.
- Exit codes: 0 = ran fine (including empty/blocked search results), 2 = bad input (bad URL, query, or flag), 1 = fetch/extraction failure.

See `references/usage.md` for copy-paste examples.
