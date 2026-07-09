"""
rss_ingest.py — RSS injury feed ingest → DNP probability map.

DNP is ultimately determined by PrizePicks removing a prop from the feed.
This RSS layer provides early-warning DNP probabilities BEFORE PP removes
the prop, so the selector can discount high-risk legs accordingly.
"""
import asyncio
import re
from typing import Dict, List

import feedparser
import httpx

from gotit.rss_feeds import RSS_FEEDS

# Pattern → max DNP probability for that keyword
INJURY_PATTERNS: List[tuple[str, float]] = [
    (r"\bout\b",              0.95),
    (r"\bruled out\b",        0.95),
    (r"\bdoubtful\b",         0.85),
    (r"\bquestionable\b",     0.60),
    (r"\binactive\b",         0.99),
    (r"\bwill not play\b",    0.95),
    (r"\bdid not play\b",     0.99),
    (r"\bmiss\b.*\bgame\b",   0.70),
    (r"\bsidelined\b",        0.80),
    (r"\bscratched\b",        0.90),
    (r"\binjury report\b",    0.30),   # soft signal — on report but status unknown
]


def normalize(name: str) -> str:
    """Strip everything except lowercase letters for fuzzy matching."""
    return re.sub(r"[^a-z]", "", name.lower())


def _extract_max_prob(text: str) -> float:
    """Return the highest injury probability found in a block of text, or 0."""
    text_lower = text.lower()
    probs = [
        prob
        for pattern, prob in INJURY_PATTERNS
        if re.search(pattern, text_lower)
    ]
    return max(probs, default=0.0)


async def _fetch_feed(url: str) -> List[dict]:
    """Fetch one RSS feed and return a list of {title, summary} dicts."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
            r.raise_for_status()
        feed = feedparser.parse(r.text)
        return [
            {
                "title":   entry.get("title", ""),
                "summary": entry.get("summary", ""),
            }
            for entry in feed.entries
        ]
    except Exception:
        return []


async def build_dnp_model(name_to_ppid: Dict[str, str]) -> Dict[str, float]:
    """
    Fetch all team RSS feeds concurrently and return a map of
    {pp_player_id: dnp_prob} for any player mentioned in an injury context.

    Args:
        name_to_ppid: {normalize(player_name): pp_player_id}
    Returns:
        {pp_player_id: max_dnp_prob_seen_across_all_feeds}
    """
    if not name_to_ppid:
        return {}

    # Pre-sort normalized names once for matching
    norm_names = list(name_to_ppid.keys())

    tasks = [_fetch_feed(url) for url in RSS_FEEDS.values()]
    feeds = await asyncio.gather(*tasks, return_exceptions=True)

    dnp_map: Dict[str, float] = {}

    for feed_result in feeds:
        if isinstance(feed_result, Exception) or not feed_result:
            continue
        for item in feed_result:
            text = f"{item['title']} {item['summary']}"
            prob = _extract_max_prob(text)
            if prob == 0.0:
                continue

            # Check if any tracked player name appears in this article
            norm_text = normalize(text)
            for norm_name in norm_names:
                if len(norm_name) < 4:
                    continue  # skip very short names — too many false positives
                if norm_name in norm_text:
                    pp_id = name_to_ppid[norm_name]
                    dnp_map[pp_id] = max(dnp_map.get(pp_id, 0.0), prob)

    return dnp_map
