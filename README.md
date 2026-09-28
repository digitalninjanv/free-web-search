# web-search

Local web search and page scraping CLI built for AI coding agents. No API keys. Anti-block HTTP via TLS-impersonated sessions, a multi-backend chain so one blocked engine does not kill the search, and token-efficient output by default.

This directory is also a valid [Agent Skill](https://agentskills.io): see `SKILL.md` for the agent-facing instructions.

## Features

- **7-backend search chain.** First backend with usable results wins; per-backend status (`ok`/`empty`/`blocked`/`error`) is reported instead of crashing.

| # | Backend | Kind | Used for |
|---|---------|------|----------|
| 1 | `bing_html` | Bing web results page | web |
| 2 | `bing_rss` | Bing RSS feed | web |
| 3 | `duckduckgo` | DuckDuckGo HTML results | web |
| 4 | `marginalia` | Marginalia JSON search API | web |
| 5 | `google_news_rss` | Google News RSS feed | news |
| 6 | `bing_news_html` | Bing News results page | news |
| 7 | `bing_news_rss` | Bing News RSS feed | news |

- **Anti-block networking.** `curl_cffi` sessions impersonating Chrome TLS fingerprints, per-request timeouts, one transient retry with backoff and jitter.
- **SSRF guard.** Only public IPs are fetched: DNS pinning, redirect re-validation, private/loopback/link-local ranges rejected.
- **Accuracy.** Term-coverage re-ranking (title weighted 3x), Jaccard near-dedup (0.85), and a relevance guard that skips backends whose results do not match the query at all.
- **Token-efficient output.** Compact numbered text by default (snippets trimmed to ~300 chars); `--urls-only` prints just URLs; `--json` gives the full structured response.
- **Scrape pagination.** Long pages are returned in chunks; re-request with `--offset <next_offset>`.

## Install

Python 3.10+.

```bash
pip install -r scripts/requirements.txt
```

Dependencies: `curl_cffi`, `trafilatura`, `lxml` (minimum versions in `scripts/requirements.txt` are the ones tested).

### Use as an agent skill

This repo is a valid [Agent Skill](https://agentskills.io): the agent reads `SKILL.md` and runs `scripts/websearch.py` for you. Install it where your agent looks for skills (the directory name must stay `web-search` so it matches the skill name).

**Pi agent** (global):

```bash
mkdir -p ~/.pi/agent/skills
unzip web-search.zip -d ~/.pi/agent/skills/web-search
# or: git clone <repo-url> ~/.pi/agent/skills/web-search
pip install -r ~/.pi/agent/skills/web-search/scripts/requirements.txt
```

**OpenCode** (global):

```bash
mkdir -p ~/.config/opencode/skills
unzip web-search.zip -d ~/.config/opencode/skills/web-search
# or: git clone <repo-url> ~/.config/opencode/skills/web-search
pip install -r ~/.config/opencode/skills/web-search/scripts/requirements.txt
```

Project-local alternative for OpenCode: `.opencode/skills/web-search/` inside your project. OpenCode also scans `~/.agents/skills/`, handy if you share skills across agents.

Restart the agent afterwards so it picks up the new skill, then verify:

```bash
python3 ~/.config/opencode/skills/web-search/scripts/websearch.py search "test" --num 1 --urls-only
```

## Quickstart

```bash
# Web search (tries Bing HTML, Bing RSS, DuckDuckGo, Marginalia in order)
python3 scripts/websearch.py search "bun javascript runtime" --num 5

# News search (Google News RSS first, then Bing News HTML/RSS)
python3 scripts/websearch.py news "gempa bumi hari ini" --num 5

# Probe before you scrape: cheap status/type/size/title check
python3 scripts/websearch.py probe https://bun.sh/docs

# Scrape readable article text (markdown by default)
python3 scripts/websearch.py scrape https://bun.sh/docs --max-chars 8000

# Cheapest discovery-then-scrape flow: URLs only, piped into scrape
python3 scripts/websearch.py search "bun javascript runtime" --num 3 --urls-only \
  | xargs -r -I{} python3 scripts/websearch.py scrape {} --max-chars 2000
```

## Commands

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `search QUERY` | Web search across the 4 web backends | `--num N` (default 5, max 50), `--freshness hour\|day\|week\|month`, `--json`, `--urls-only` |
| `news QUERY` | News search across the 3 news backends | same as `search` |
| `probe URL` | Cheap pre-scrape check: HTTP status, content type, size, title | `--json` |
| `scrape URL` | Extract readable article text | `--max-chars N` (default 8000), `--offset N`, `--format markdown\|txt\|xml\|json`, `--json` |

Run `python3 scripts/websearch.py <command> --help` for full flag details.

## Output contract

- Default output is compact text. `--json` prints the full structured response instead.
- Search response keys: `query`, `source_type`, `status`, `backend`, `message`, `results`. Result keys: `title`, `url`, `snippet`, `backend`, `score` (plus `source`/`age` for news).
- `status` is `ok`, `empty`, `blocked`, or `error`. Non-`ok` statuses are normal outcomes, not failures: narrow the query or retry later.
- Exit codes: `0` = ran fine (including empty/blocked results), `2` = bad input (bad URL, query, or flag), `1` = fetch/extraction failure.

## Testing

```bash
python3 -m pytest tests/ -q
```

Fixture-based tests run offline. Live-network tests are opt-in and skipped by default:

```bash
WEBSEARCH_LIVE=1 python3 -m pytest tests/ -q
```

CI (`.github/workflows/test.yml`) runs the offline suite on Python 3.10 and 3.12.

## Project layout

```
web-search/
  SKILL.md                  Agent Skill instructions (name: web-search)
  README.md                 This file
  LICENSE                 MIT
  scripts/
    websearch.py            Single-file CLI (stdlib + curl_cffi/trafilatura/lxml)
    requirements.txt        Pinned minimum dependencies
  references/
    usage.md                Copy-paste examples for agents
  tests/
    test_websearch.py       Fixture-based test suite
    fixtures/               Captured real backend responses
  .github/workflows/       CI
```

## Notes

- Search backends are third-party services; availability and blocking behavior change over time. The chain and the structured `blocked` status exist so callers degrade gracefully.
- Google News RSS article links point at `news.google.com`; the fetcher follows redirects where the publisher allows it.
