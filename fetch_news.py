#!/usr/bin/env python3
"""
IP Phone News Aggregator - Fetches RSS feeds, filters by keywords, generates data.json
"""

import feedparser
import json
import re
import hashlib
from datetime import datetime, timezone
from time import mktime
import urllib.request
import urllib.error
import ssl

# RSS Feed sources
FEEDS = [
    {"name": "No Jitter", "url": "https://www.nojitter.com/rss.xml", "icon": "📡"},
    {"name": "UC Today", "url": "https://www.uctoday.com/feed/", "icon": "📰"},
    {"name": "TechTarget UC", "url": "https://www.techtarget.com/searchunifiedcommunications/rss/ContentSyndication.xml", "icon": "🔍"},
    {"name": "Telecom Reseller", "url": "https://telecomreseller.com/feed/", "icon": "📞"},
    {"name": "Reddit r/VOIP", "url": "https://www.reddit.com/r/VOIP/.rss", "icon": "💬"},
    {"name": "VoIP Info", "url": "https://www.voip-info.org/feed/", "icon": "ℹ️"},
]

# Brand keywords to filter and tag
BRANDS = ["Cisco", "Poly", "Polycom", "Avaya", "Microsoft Teams", "Yealink", "Mitel"]

# Broader IP phone related keywords
IP_PHONE_KEYWORDS = [
    "ip phone", "voip", "sip phone", "desk phone", "video phone",
    "conference phone", "unified communications", "ucaas", "pbx",
    "ip telephony", "softphone", "webex", "zoom phone", "teams phone",
    "ip phone", "dect", "call center", "contact center",
    "cisco phone", "poly phone", "avaya phone", "yealink phone",
    "mitel phone", "polycom phone", "rps", "auto provisioning",
    "phone system", "business phone", "enterprise phone",
    "collaboration", "video conferencing",
]


def fetch_feed(feed_info):
    """Fetch and parse a single RSS feed."""
    entries = []
    try:
        # Create SSL context that doesn't verify (some feeds have cert issues)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            feed_info["url"],
            headers={"User-Agent": "Mozilla/5.0 (IPPhoneAggregator/1.0)"}
        )
        response = urllib.request.urlopen(req, timeout=15, context=ctx)
        content = response.read()
        feed = feedparser.parse(content)

        for entry in feed.entries[:30]:  # Limit per feed
            # Extract date
            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_date = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc).isoformat()
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                pub_date = datetime.fromtimestamp(mktime(entry.updated_parsed), tz=timezone.utc).isoformat()

            # Extract summary/description
            summary = ""
            if hasattr(entry, "summary"):
                summary = re.sub(r"<[^>]+>", "", entry.summary)[:300]
            elif hasattr(entry, "description"):
                summary = re.sub(r"<[^>]+>", "", entry.description)[:300]

            title = entry.get("title", "Untitled")
            link = entry.get("link", "")

            # Generate unique ID
            uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()[:12]

            entries.append({
                "id": uid,
                "title": title,
                "link": link,
                "summary": summary.strip(),
                "date": pub_date,
                "source": feed_info["name"],
                "icon": feed_info["icon"],
            })

        print(f"  ✓ {feed_info['name']}: {len(entries)} entries")
    except Exception as e:
        print(f"  ✗ {feed_info['name']}: {e}")

    return entries


def match_brands(text):
    """Find which brands are mentioned in text."""
    text_lower = text.lower()
    matched = []
    for brand in BRANDS:
        if brand.lower() in text_lower:
            matched.append(brand)
    return matched


def is_relevant(entry):
    """Check if an entry is related to IP phones / UC."""
    text = f"{entry['title']} {entry['summary']}".lower()
    # Check brand keywords
    for brand in BRANDS:
        if brand.lower() in text:
            return True
    # Check IP phone keywords
    for kw in IP_PHONE_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


def main():
    print("🔄 Fetching IP Phone news...")
    all_entries = []

    for feed_info in FEEDS:
        entries = fetch_feed(feed_info)
        all_entries.extend(entries)

    print(f"\n📊 Total raw entries: {len(all_entries)}")

    # Filter relevant entries
    relevant = [e for e in all_entries if is_relevant(e)]
    print(f"📋 Relevant entries after filtering: {len(relevant)}")

    # If too few relevant results, include all entries
    if len(relevant) < 10:
        print("⚠️  Few relevant results, including all entries")
        relevant = all_entries

    # Tag with brands
    for entry in relevant:
        text = f"{entry['title']} {entry['summary']}"
        entry["brands"] = match_brands(text)

    # Sort by date (newest first)
    relevant.sort(key=lambda x: x.get("date") or "1970-01-01", reverse=True)

    # Deduplicate by title similarity
    seen_titles = set()
    unique = []
    for entry in relevant:
        title_key = re.sub(r"[^a-z0-9]", "", entry["title"].lower())[:50]
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(entry)

    # Build output
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "total": len(unique),
        "sources": [f["name"] for f in FEEDS],
        "brands": BRANDS,
        "entries": unique[:100],  # Limit to 100 entries
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Saved {len(unique)} entries to data.json")
    print(f"🕐 Updated at: {output['updated']}")


if __name__ == "__main__":
    main()
