#!/usr/bin/env python3
"""
IP Phone 信息聚合
从 UC/VoIP 行业网站抓取最新新闻，生成静态 HTML 页面到 docs/index.html。

数据来源：
  1. No Jitter           nojitter.com
  2. UC Today             uctoday.com
  3. TechTarget UC        techtarget.com/searchunifiedcommunications
  4. Telecom Reseller     telecomreseller.com
  5. Reddit r/VOIP        reddit.com/r/VOIP
  6. VoIP Info            voip-info.org
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
TIMEOUT = 15
MAX_ITEMS_PER_SOURCE = 8

# ── 数据来源 ──────────────────────────────────────────────
SOURCES = [
    {"name": "No Jitter",        "category": "UC",  "url": "https://www.nojitter.com/rss.xml"},
    {"name": "UC Today",         "category": "UC",  "url": "https://www.uctoday.com/feed/"},
    {"name": "TechTarget UC",    "category": "企业", "url": "https://www.techtarget.com/searchunifiedcommunications/rss/ContentSyndication.xml"},
    {"name": "Telecom Reseller", "category": "电信", "url": "https://telecomreseller.com/feed/"},
    {"name": "Reddit r/VOIP",    "category": "社区", "url": "https://www.reddit.com/r/VOIP/.rss"},
    {"name": "VoIP Info",        "category": "VoIP", "url": "https://www.voip-info.org/feed/"},
]

SOURCE_URLS = {
    "No Jitter":        "https://www.nojitter.com",
    "UC Today":         "https://www.uctoday.com",
    "TechTarget UC":    "https://www.techtarget.com/searchunifiedcommunications",
    "Telecom Reseller": "https://telecomreseller.com",
    "Reddit r/VOIP":    "https://www.reddit.com/r/VOIP",
    "VoIP Info":        "https://www.voip-info.org",
}

# ── 品牌与关键词 ──────────────────────────────────────────
BRANDS = ["Cisco", "Poly", "Polycom", "Avaya", "Microsoft Teams", "Yealink", "Mitel"]

KEYWORDS = [
    "ip phone", "voip", "sip phone", "desk phone", "video phone",
    "conference phone", "unified communications", "ucaas", "pbx",
    "ip telephony", "softphone", "webex", "zoom phone", "teams phone",
    "dect", "call center", "contact center",
    "cisco phone", "poly phone", "avaya phone", "yealink phone",
    "mitel phone", "polycom phone", "auto provisioning",
    "phone system", "business phone", "enterprise phone",
    "collaboration", "video conferencing", "rps",
] + [b.lower() for b in BRANDS]

_KW_PATTERN = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)


# ── 辅助函数 ──────────────────────────────────────────────

def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, n: int = 400) -> str:
    return text[:n] + "…" if len(text) > n else text


def _matches_keywords(item: dict) -> bool:
    text = item.get("title", "") + " " + item.get("summary", "")
    return bool(_KW_PATTERN.search(text))


def _match_brands(text: str) -> list[str]:
    text_lower = text.lower()
    return [b for b in BRANDS if b.lower() in text_lower]


def _fetch_og_description(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        og = soup.find("meta", property="og:description")
        if og and og.get("content", "").strip():
            desc = _clean(og["content"])
            if len(desc) > 40:
                return desc
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content", "").strip():
            desc = _clean(meta["content"])
            if len(desc) > 40:
                return desc
        paragraphs = []
        for p in soup.select("article p, .content p, .entry-content p, main p"):
            t = _clean(p.get_text())
            if len(t) > 40:
                paragraphs.append(t)
                if sum(len(x) for x in paragraphs) >= 300:
                    break
        if paragraphs:
            return " ".join(paragraphs)
    except Exception:
        pass
    return ""


# ── 时间解析 ──────────────────────────────────────────────

_TIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%b %d, %Y",
    "%B %d, %Y",
]


def _parse_time(time_str: str) -> datetime | None:
    if not time_str:
        return None
    time_str = time_str.strip()
    try:
        dt = parsedate_to_datetime(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# ── RSS 抓取 ──────────────────────────────────────────────

def _fetch_rss(source: dict) -> list[dict]:
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("✘ %-18s  失败: %s", source["name"], exc)
        return []

    results = []
    for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        pub = ""
        if hasattr(e, "published"):
            pub = e.published
        elif hasattr(e, "updated"):
            pub = e.updated
        results.append({
            "category": source["category"],
            "title": _clean(e.get("title", "")),
            "summary": _truncate(_clean(e.get("summary", e.get("description", "")))),
            "source": source["name"],
            "link": e.get("link", ""),
            "time": pub,
        })
    log.info("✔ %-18s  %d 条", source["name"], len(results))
    return results


# ── 聚合 ─────────────────────────────────────────────────

def fetch_all() -> list[dict]:
    all_news: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_rss, s): s for s in SOURCES}
        for fut in as_completed(futures):
            all_news.extend(fut.result())

    # 过滤
    filtered = [n for n in all_news if _matches_keywords(n)]
    log.info("关键字过滤: %d / %d 条匹配", len(filtered), len(all_news))

    # 如果匹配太少，保留全部
    if len(filtered) < 10:
        log.info("⚠️ 匹配过少，保留全部 %d 条", len(all_news))
        filtered = all_news

    # 补充短摘要
    need_enrich = [n for n in filtered if len(n.get("summary", "")) < 100 and n.get("link")]
    if need_enrich:
        log.info("补充摘要: %d 条需从原文获取…", len(need_enrich))
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {pool.submit(_fetch_og_description, n["link"]): n for n in need_enrich}
            for fut in as_completed(future_map):
                item = future_map[fut]
                desc = fut.result()
                if desc and len(desc) > len(item.get("summary", "")):
                    item["summary"] = _truncate(desc)

    # 标记品牌
    for n in filtered:
        n["brands"] = _match_brands(n["title"] + " " + n.get("summary", ""))

    # 解析时间、过滤最近两周、排序
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=2)
    for n in filtered:
        n["_dt"] = _parse_time(n.get("time", ""))

    filtered = [n for n in filtered if n["_dt"] is None or n["_dt"] >= cutoff]
    filtered.sort(key=lambda n: n["_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # 去重
    seen = set()
    unique = []
    for n in filtered:
        key = re.sub(r"[^a-z0-9]", "", n["title"].lower())[:50]
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique


# ── HTML 生成 ─────────────────────────────────────────────

CATEGORY_COLORS = {
    "UC":   ("#e3f2fd", "#1565c0"),
    "企业": ("#e8f5e9", "#2e7d32"),
    "电信": ("#fff3e0", "#e65100"),
    "社区": ("#fce4ec", "#c62828"),
    "VoIP": ("#ede7f6", "#4527a0"),
}

BRAND_COLORS = {
    "Cisco":           ("#e3f2fd", "#049fd9"),
    "Poly":            ("#f3e5f5", "#7b1fa2"),
    "Polycom":         ("#f3e5f5", "#7b1fa2"),
    "Avaya":           ("#ffebee", "#c62828"),
    "Microsoft Teams": ("#e8eaf6", "#5c6bc0"),
    "Yealink":         ("#e8f5e9", "#2e7d32"),
    "Mitel":           ("#e3f2fd", "#01579b"),
}


def _category_badge(cat: str) -> str:
    bg, fg = CATEGORY_COLORS.get(cat, ("#eeeeee", "#333333"))
    return f'<span class="badge" style="background:{bg};color:{fg}">{html_mod.escape(cat)}</span>'


def _brand_tags(brands: list[str]) -> str:
    if not brands:
        return ""
    tags = []
    for b in brands:
        bg, fg = BRAND_COLORS.get(b, ("#eeeeee", "#333"))
        tags.append(f'<span class="brand-tag" style="background:{bg};color:{fg}">{html_mod.escape(b)}</span>')
    return '<div class="brand-tags">' + " ".join(tags) + "</div>"


def _news_card(item: dict) -> str:
    title = html_mod.escape(item["title"])
    summary = html_mod.escape(item.get("summary", ""))
    source = html_mod.escape(item["source"])
    time_str = html_mod.escape(item.get("time", ""))
    link = item.get("link", "")

    title_html = (
        f'<a href="{html_mod.escape(link)}" target="_blank" rel="noopener">{title}</a>'
        if link else title
    )

    meta_parts = [source]
    if time_str:
        meta_parts.append(time_str)

    badge = _category_badge(item["category"])
    meta = " · ".join(meta_parts)
    brands_html = _brand_tags(item.get("brands", []))

    return (
        '    <article class="card">\n'
        '      <div class="card-header">\n'
        f'        {badge}\n'
        f'        <span class="meta">{meta}</span>\n'
        '      </div>\n'
        f'      <h3 class="card-title">{title_html}</h3>\n'
        f'      <p class="card-summary">{summary}</p>\n'
        f'      {brands_html}\n'
        '    </article>'
    )


def _source_list_html() -> str:
    items = []
    for name, url in SOURCE_URLS.items():
        items.append(f'<a href="{url}" target="_blank" rel="noopener" class="src-tag">{html_mod.escape(name)}</a>')
    return " ".join(items)


def generate_html(news: list[dict] | None = None) -> str:
    if news is None:
        news = fetch_all()

    cards_html = "\n".join(_news_card(n) for n in news)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(news)
    sources_html = _source_list_html()

    return f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>IP Phone</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
                   system-ui, -apple-system, sans-serif;
      background: #f0f2f5;
      color: #333;
      line-height: 1.6;
    }}

    header {{
      background: linear-gradient(135deg, #0d47a1 0%, #01579b 50%, #006064 100%);
      color: #fff;
      padding: 32px 24px 22px;
      text-align: center;
      box-shadow: 0 2px 10px rgba(0,0,0,.2);
    }}
    header h1 {{
      font-size: 1.9rem;
      font-weight: 700;
      letter-spacing: .08em;
      display: inline;
    }}
    .header-row {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 16px;
    }}
    .btn-refresh {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 18px;
      font-size: .85rem;
      font-weight: 600;
      color: #0d47a1;
      background: #fff;
      border: none;
      border-radius: 20px;
      cursor: pointer;
      transition: background .2s, transform .15s;
      box-shadow: 0 2px 6px rgba(0,0,0,.15);
      white-space: nowrap;
    }}
    .btn-refresh:hover {{ background: #e3f2fd; }}
    .btn-refresh:active {{ transform: scale(.95); }}
    .btn-refresh .icon {{
      display: inline-block;
      transition: transform .4s;
    }}
    .btn-refresh.loading {{
      pointer-events: none;
      opacity: .7;
    }}
    .btn-refresh.loading .icon {{
      animation: spin 1s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    header .subtitle {{
      margin-top: 8px;
      font-size: .85rem;
      opacity: .85;
    }}

    .sources {{
      background: #fff;
      padding: 14px 24px;
      text-align: center;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
      overflow-x: auto;
      white-space: nowrap;
    }}
    .sources .label {{
      font-size: .8rem;
      color: #888;
      margin-right: 8px;
    }}
    .src-tag {{
      display: inline-block;
      font-size: .75rem;
      padding: 3px 10px;
      margin: 3px 4px;
      border-radius: 14px;
      background: #e3f2fd;
      color: #1565c0;
      text-decoration: none;
      transition: background .15s;
    }}
    .src-tag:hover {{ background: #bbdefb; }}

    .container {{
      max-width: 860px;
      margin: 24px auto;
      padding: 0 16px;
    }}
    .stats {{
      text-align: center;
      font-size: .82rem;
      color: #999;
      margin-bottom: 16px;
    }}

    .card {{
      background: #fff;
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 14px;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
      transition: transform .18s, box-shadow .18s;
    }}
    .card:hover {{
      transform: translateY(-3px);
      box-shadow: 0 6px 18px rgba(0,0,0,.1);
    }}
    .card-header {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .badge {{
      display: inline-block;
      font-size: .73rem;
      font-weight: 600;
      padding: 2px 10px;
      border-radius: 12px;
    }}
    .meta {{
      font-size: .76rem;
      color: #999;
    }}
    .card-title {{
      font-size: 1.05rem;
      font-weight: 600;
      margin-bottom: 6px;
    }}
    .card-title a {{
      color: #222;
      text-decoration: none;
    }}
    .card-title a:hover {{
      color: #0d47a1;
      text-decoration: underline;
    }}
    .card-summary {{
      font-size: .9rem;
      color: #555;
    }}
    .brand-tags {{
      margin-top: 10px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .brand-tag {{
      display: inline-block;
      font-size: .7rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
    }}

    .empty {{
      text-align: center;
      padding: 60px 20px;
      color: #aaa;
      font-size: 1rem;
    }}

    footer {{
      text-align: center;
      padding: 24px 16px;
      font-size: .78rem;
      color: #aaa;
    }}

    @media (max-width: 600px) {{
      header h1 {{ font-size: 1.4rem; }}
      .card {{ padding: 16px; }}
    }}
  </style>
</head>
<body>

  <header>
    <div class="header-row">
      <h1>📞 IP Phone</h1>
      <button class="btn-refresh" id="btnRefresh">
        <span class="icon">&#x21bb;</span> 刷新
      </button>
    </div>
    <p class="subtitle">实时聚合 UC/VoIP 行业权威媒体 · 最后更新：{now}（每6小时自动刷新）</p>
  </header>

  <div class="sources">
    <span class="label">数据来源：</span>
    {sources_html}
  </div>

  <main class="container">
    <p class="stats">共聚合 {total} 条新闻</p>
{cards_html}
  </main>

  <footer>
    IP Phone 信息聚合 &copy; 2026
  </footer>

  <script>
  var DISPATCH_TOKEN = '';
  var REPO = 'dabch2020/ipphone';
  var btn = document.querySelector('.btn-refresh');
  var subtitleSpan = document.querySelector('.subtitle');

  btn.onclick = function() {{
    btn.classList.add('loading');
    subtitleSpan.textContent = '已触发更新，正在等待构建完成…';

    // 尝试通过 repository_dispatch 触发 GitHub Actions
    if (DISPATCH_TOKEN) {{
      fetch('https://api.github.com/repos/' + REPO + '/dispatches', {{
        method: 'POST',
        headers: {{
          'Authorization': 'Bearer ' + DISPATCH_TOKEN,
          'Accept': 'application/vnd.github.v3+json'
        }},
        body: JSON.stringify({{ event_type: 'refresh' }})
      }})
      .then(function(r) {{
        if (r.status === 204 || r.status === 200) {{
          subtitleSpan.textContent = '✅ 已触发更新，正在等待构建完成…';
          pollForUpdate();
        }} else {{
          subtitleSpan.textContent = '❌ 触发失败 (HTTP ' + r.status + ')';
          btn.classList.remove('loading');
        }}
      }})
      .catch(function(e) {{
        subtitleSpan.textContent = '❌ 网络错误，请稍后重试';
        btn.classList.remove('loading');
      }});
    }} else {{
      // 无 token 时直接刷新页面
      subtitleSpan.textContent = '正在刷新页面…';
      setTimeout(function() {{ location.reload(); }}, 1500);
    }}
  }};

  function pollForUpdate() {{
    var originalTime = '{now}';
    var attempts = 0;
    var maxAttempts = 24;
    var timer = setInterval(function() {{
      attempts++;
      fetch(location.href.split('?')[0] + '?_t=' + Date.now())
        .then(function(r) {{ return r.text(); }})
        .then(function(html) {{
          var m = html.match(/最后更新：([^（]+)/);
          if (m && m[1].trim() !== originalTime) {{
            clearInterval(timer);
            location.reload();
          }} else if (attempts >= maxAttempts) {{
            clearInterval(timer);
            subtitleSpan.textContent = '✅ 构建已触发，请稍后手动刷新页面';
            btn.classList.remove('loading');
          }}
        }})
        .catch(function() {{}});
    }}, 5000);
  }}
  </script>

</body>
</html>"""


def main():
    log.info("🔄 开始抓取 IP Phone 新闻…")
    news = fetch_all()
    log.info("📊 最终 %d 条新闻", len(news))

    html = generate_html(news)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    log.info("✅ 已生成 %s (%d 字节)", out_file, len(html))


if __name__ == "__main__":
    main()
