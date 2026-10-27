"""Web search through a provider the user configures.

Providers, tried in this order when the setting is "auto":
- brave:     Brave Search API. Needs your own API key (Settings → Tools; stored in
             Windows Credential Manager). Brave bills per search (prepaid credits, a
             monthly free credit); Enoy stops using it after tools.brave_monthly_limit
             searches in a calendar month and falls back to the next provider.
- searxng:   a SearXNG instance you run or trust, by URL (JSON output enabled).
- wikipedia: Wikipedia's own search API. No key, but it only finds encyclopedia
             articles: good for background, not for news or today's events.
Results say which provider produced them, so an answer can be honest about it.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.parse

from . import keystore
from .registry import Tool, ToolContext, ToolError
from .web import get_json, http_get

BRAVE = "https://api.search.brave.com/res/v1/web/search?q={q}&count={n}"
WIKI = "https://{lang}.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit={n}&srprop=snippet|timestamp&srsearch={q}"


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


_usage_lock = threading.Lock()


def _usage_file():
    from .. import paths
    return paths.app_home() / "brave-usage.json"


def brave_usage() -> dict:
    """Searches sent to Brave this calendar month (counted by Enoy, not reported by Brave)."""
    month = time.strftime("%Y-%m")
    try:
        data = json.loads(_usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"month": month, "count": int(data.get("count", 0)) if data.get("month") == month else 0}


def _count_brave(limit: int) -> None:
    with _usage_lock:
        usage = brave_usage()
        if usage["count"] >= limit:
            raise ToolError("unavailable", f"Brave Search reached Enoy's monthly limit ({limit} searches); "
                                           "change it in Settings → Tools.")
        usage["count"] += 1
        try:
            _usage_file().write_text(json.dumps(usage), encoding="utf-8")
        except OSError:
            pass


def _brave(q: str, n: int, ctx: ToolContext) -> list[dict]:
    key = keystore.get("brave_search")
    if not key:
        raise ToolError("unavailable", "No Brave Search key is saved.")
    _count_brave(int(ctx.cfg.get("tools", {}).get("brave_monthly_limit", 900)))
    body, info = http_get(BRAVE.format(q=urllib.parse.quote(q), n=n), ctx.cfg, ctx.cancel, timeout=12,
                          accept="application/json", headers={"X-Subscription-Token": key})
    data = json.loads(body.decode("utf-8", errors="replace"))
    out = []
    for r in (data.get("web") or {}).get("results", [])[:n]:
        out.append({"title": _clean(r.get("title")), "url": r.get("url"), "snippet": _clean(r.get("description")),
                    "published": r.get("page_age") or r.get("age") or ""})
    return out


def _searxng(q: str, n: int, ctx: ToolContext) -> list[dict]:
    base = (ctx.cfg.get("tools", {}).get("searxng_url") or "").rstrip("/")
    if not base:
        raise ToolError("unavailable", "No SearXNG address is set.")
    data = get_json(f"{base}/search?format=json&q={urllib.parse.quote(q)}", ctx.cfg, ctx.cancel, timeout=12)
    return [{"title": _clean(r.get("title")), "url": r.get("url"), "snippet": _clean(r.get("content")),
             "published": r.get("publishedDate") or ""} for r in data.get("results", [])[:n]]


def _wikipedia(q: str, n: int, ctx: ToolContext) -> list[dict]:
    lang = ctx.cfg.get("tools", {}).get("wikipedia_lang") or "en"
    data = get_json(WIKI.format(lang=lang, n=n, q=urllib.parse.quote(q)), ctx.cfg, ctx.cancel, timeout=12)
    out = []
    for r in data.get("query", {}).get("search", [])[:n]:
        title = r.get("title", "")
        out.append({"title": title, "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                    "snippet": _clean(r.get("snippet")), "published": "",
                    "last_edited": r.get("timestamp", "")})
    return out


PROVIDERS = {"brave": _brave, "searxng": _searxng, "wikipedia": _wikipedia}
NOTES = {"wikipedia": "Wikipedia search only finds encyclopedia articles. It is background, not current news; "
                      "say so if the user asked about recent events."}


def configured(cfg: dict) -> list[str]:
    s = cfg.get("tools", {})
    choice = s.get("search_provider", "auto")
    if choice != "auto":
        return [choice]
    order = []
    if keystore.get("brave_search"):
        order.append("brave")
    if s.get("searxng_url"):
        order.append("searxng")
    order.append("wikipedia")
    return order


def _search(args: dict, ctx: ToolContext) -> dict:
    q = args["query"]
    n = int(args.get("count", 5))
    errors = []
    for name in configured(ctx.cfg):
        if ctx.cancel.is_set():
            raise ToolError("cancelled", "Stopped.")
        try:
            results = PROVIDERS[name](q, n, ctx)
        except ToolError as exc:
            errors.append(f"{name}: {exc}")
            continue
        now = time.strftime("%Y-%m-%d %H:%M")
        out = {"query": q, "provider": name, "results": results,
               "_sources": [{"title": r["title"], "url": r["url"], "retrieved": now} for r in results if r.get("url")]}
        if name in NOTES:
            out["note"] = NOTES[name]
        if errors:
            out["fallback_used"] = "; ".join(errors)
        if not results:
            out["note"] = (out.get("note", "") + " No results.").strip()
        return out
    raise ToolError("unavailable", "No search provider answered. " + " | ".join(errors))


WEB_SEARCH = Tool(
    name="web_search",
    description="Search the web. Returns titles, URLs, snippets and dates when known. Then open the most "
                "relevant results with fetch_page before relying on them. Run another search with different "
                "words if the first one is not enough.",
    parameters={"type": "object", "properties": {
        "query": {"type": "string", "description": "Search words.", "maxLength": 300},
        "count": {"type": "integer", "description": "Number of results (default 5).", "minimum": 1, "maximum": 10}},
        "required": ["query"]},
    run=_search,
    output="query, provider used, results (title, url, snippet, published), note on limits",
    needs_internet=True, permission="web", timeout_s=20, cache_ttl_s=900, attribution=True,
)
