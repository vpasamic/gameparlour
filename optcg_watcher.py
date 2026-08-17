#!/usr/bin/env python3
"""
optcg_watcher.py
================
Watches The Game Parlour's event calendar (and any other pages you add) for
One Piece TCG events, and pings a Discord webhook when a *new* one appears.

The Game Parlour runs its event calendar on Square Online, which renders its
product grid with JavaScript. So this script has two fetch modes:

  * "http"     - plain requests. Fast. Works if the page ships server-side HTML.
  * "render"   - Playwright headless Chromium. Slower but handles JS pages.
                 This is the default for square.site sources.

Usage
-----
    python optcg_watcher.py --once            # one pass, notify on new events
    python optcg_watcher.py --loop 1800       # poll every 30 min
    python optcg_watcher.py --dump            # print what it scraped, no ping
    python optcg_watcher.py --test-webhook    # verify the Discord side works
    python optcg_watcher.py --once --prime    # record current events, don't ping

Config via environment variables (or a .env-style export):
    DISCORD_WEBHOOK_URL   required
    DISCORD_ROLE_ID       optional, pings <@&ROLE_ID> on new events
    STATE_FILE            optional, default ./seen_events.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SOURCES = [
    # (label, url, fetch_mode)
    ("Game Parlour — Event Calendar",
     "https://thegameparloursf.square.site/shop/event-calendar/23",
     "render"),
    ("Game Parlour — Gaming Events",
     "https://thegameparloursf.square.site/shop/gaming-events/23",
     "render"),
]

# Case-insensitive (the regex compiles with re.IGNORECASE), so "one piece",
# "One Piece", "OP TCG" and "op tcg" are all handled by the same patterns.
#
# The last pattern matches a standalone "OP". It uses a lookbehind/lookahead
# for hyphens instead of plain \b, because \bop\b would also fire on the "op"
# in "Co-op Board Game Night" — which a board game cafe definitely runs.
KEYWORDS = [
    r"one\s*piece",
    r"\bop\s*tcg\b",
    r"\boptcg\b",
    r"(?<![-\w])op(?![-\w])",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_events.json"))
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)

# Nav/footer junk that shows up as links on Square Online storefronts.
IGNORE_TITLES = {
    "", "shop", "home", "cart", "search", "menu", "about", "contact",
    "log in", "sign in", "checkout", "all products", "back",
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    title: str
    url: str
    source: str
    detail: str = ""

    @property
    def key(self) -> str:
        """Stable dedup key. Uses the URL when there is one, else the title."""
        basis = self.url or f"{self.source}|{self.title.lower().strip()}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_http(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_rendered(url: str, timeout_ms: int = 30000) -> str:
    """Render with Playwright. Requires: pip install playwright && playwright install chromium"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[warn] playwright not installed; falling back to plain HTTP for", url,
              file=sys.stderr)
        return fetch_http(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # Square Online lazy-loads the product grid; nudge it and give it a beat.
        try:
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        html = page.content()
        browser.close()
    return html


def fetch(url: str, mode: str) -> str:
    return fetch_rendered(url) if mode == "render" else fetch_http(url)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def extract_events(html: str, base_url: str, source: str) -> list[Event]:
    """
    Pull candidate event entries out of the page.

    Strategy is deliberately loose because storefront markup changes often:
      1. Anchors pointing at product/item/event detail pages.
      2. Any remaining text node that matches a keyword, as a safety net.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    found: dict[str, Event] = {}

    # --- 1. Product / event detail links ---------------------------------
    link_pat = re.compile(r"/(product|item|event|shop)/", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not link_pat.search(href):
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        if title.lower() in IGNORE_TITLES or len(title) < 4:
            continue
        full = urljoin(base_url, href)
        ev = Event(title=title, url=full, source=source)
        found[ev.key] = ev

    # --- 2. Text-node safety net -----------------------------------------
    if not found:
        text = soup.get_text("\n", strip=True)
        for line in text.splitlines():
            line = " ".join(line.split())
            if 4 < len(line) < 200 and KEYWORD_RE.search(line):
                ev = Event(title=line, url=base_url, source=source)
                found.setdefault(ev.key, ev)

    return list(found.values())


def filter_one_piece(events: list[Event]) -> list[Event]:
    return [e for e in events
            if KEYWORD_RE.search(e.title) or KEYWORD_RE.search(e.url)]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print(f"[warn] {STATE_FILE} was corrupt; starting fresh", file=sys.stderr)
    return {"seen": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def clean_role_id(role_id: str | None) -> str | None:
    """Discord role IDs are 17-20 digit snowflakes. Anything else breaks the API."""
    if not role_id:
        return None
    rid = role_id.strip().strip("<>@&")
    if rid.isdigit() and 17 <= len(rid) <= 20:
        return rid
    print(f"[warn] DISCORD_ROLE_ID is not a valid role ID (got {len(rid)} chars); "
          "ignoring it. Enable Developer Mode in Discord, right-click the role, "
          "Copy ID.", file=sys.stderr)
    return None


def post_discord(webhook: str, content: str, embeds: list[dict] | None = None,
                 role_id: str | None = None) -> None:
    rid = clean_role_id(role_id)
    payload = {
        "content": content,
        "username": "Grand Line Watch",
        "allowed_mentions": {"roles": [rid] if rid else [], "parse": []},
    }
    if embeds:
        # Discord rejects null fields, so drop any key whose value is None.
        payload["embeds"] = [{k: v for k, v in e.items() if v is not None}
                             for e in embeds[:10]]

    resp = requests.post(webhook.strip(), json=payload, timeout=15)
    if resp.status_code == 429:
        retry = resp.json().get("retry_after", 5)
        time.sleep(float(retry) + 0.5)
        resp = requests.post(webhook.strip(), json=payload, timeout=15)

    if not resp.ok:
        # Discord's body explains exactly which field it disliked. Print it,
        # otherwise you just get an opaque "400 Bad Request".
        print(f"[error] Discord returned {resp.status_code}: {resp.text[:800]}",
              file=sys.stderr)
    resp.raise_for_status()


def notify(webhook: str, events: list[Event], role_id: str | None) -> None:
    mention = f"<@&{role_id}> " if role_id else ""
    plural = "event" if len(events) == 1 else "events"
    header = f"{mention}🏴‍☠️ **{len(events)} new One Piece {plural}** at The Game Parlour"

    embeds = [{
        "title": e.title[:250],
        "url": e.url,
        "description": e.detail[:400] or None,
        "color": 0xD32F2F,
        "footer": {"text": e.source},
    } for e in events]

    # Chunk into groups of 10 so nothing gets silently dropped.
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        post_discord(webhook, header if i == 0 else "", chunk, role_id)
        time.sleep(1)


# --------------------------------------------------------------------------
# Main pass
# --------------------------------------------------------------------------

def run_once(webhook: str | None, role_id: str | None,
             dump: bool = False, prime: bool = False) -> int:
    state = load_state()
    seen: dict = state.setdefault("seen", {})
    new_events: list[Event] = []
    all_scraped: list[Event] = []

    for label, url, mode in SOURCES:
        try:
            html = fetch(url, mode)
        except Exception as exc:
            print(f"[error] fetching {url}: {exc}", file=sys.stderr)
            continue

        scraped = extract_events(html, url, label)
        all_scraped.extend(scraped)
        matches = filter_one_piece(scraped)
        print(f"[info] {label}: {len(scraped)} entries, {len(matches)} One Piece")

        for ev in matches:
            if ev.key not in seen:
                new_events.append(ev)

    if dump:
        print("\n--- everything scraped ---")
        for ev in all_scraped:
            flag = "OP" if KEYWORD_RE.search(ev.title) else "  "
            print(f"  [{flag}] {ev.title}  ->  {ev.url}")
        print(f"\n{len(all_scraped)} total entries. Nothing sent.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    for ev in new_events:
        seen[ev.key] = {**asdict(ev), "first_seen": now}
    state["last_run"] = now
    save_state(state)

    if not new_events:
        print("[info] no new One Piece events")
        return 0

    if prime:
        print(f"[info] primed {len(new_events)} existing events (no ping sent)")
        return 0

    if not webhook:
        print("[error] DISCORD_WEBHOOK_URL not set; found but could not send:",
              file=sys.stderr)
        for ev in new_events:
            print("   ", ev.title, ev.url, file=sys.stderr)
        return 1

    notify(webhook, new_events, role_id)
    print(f"[info] pinged Discord about {len(new_events)} new event(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="One Piece TCG event watcher")
    ap.add_argument("--once", action="store_true", help="run a single pass (default)")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="poll forever, sleeping SECONDS between passes")
    ap.add_argument("--dump", action="store_true",
                    help="print everything scraped and exit; sends nothing")
    ap.add_argument("--prime", action="store_true",
                    help="record current events as already-seen without pinging")
    ap.add_argument("--test-webhook", action="store_true",
                    help="send a test message to the webhook and exit")
    args = ap.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    role_id = os.environ.get("DISCORD_ROLE_ID") or None

    if args.test_webhook:
        if not webhook:
            print("[error] DISCORD_WEBHOOK_URL not set", file=sys.stderr)
            return 1
        post_discord(webhook, "🏴‍☠️ Grand Line Watch is online. Test ping.", role_id=role_id)
        print("[info] test message sent")
        return 0

    if args.loop:
        while True:
            try:
                run_once(webhook, role_id, dump=args.dump, prime=args.prime)
            except Exception as exc:
                print(f"[error] pass failed: {exc}", file=sys.stderr)
            args.prime = False  # only prime on the first pass
            time.sleep(args.loop)

    return run_once(webhook, role_id, dump=args.dump, prime=args.prime)


if __name__ == "__main__":
    sys.exit(main())
