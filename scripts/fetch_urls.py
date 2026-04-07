#!/usr/bin/env python3
"""
vibe-rss URL fetcher: 并发抓取sources.txt中所有URL的最新内容。
优先RSS/Atom feed，fallback到HTML scraping。HN使用专用API含评论。

Usage: python3 fetch_urls.py <sources.txt>
"""

import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp

# ==== Config ====
CONCURRENCY     = 20
TIMEOUT         = 45
MAX_BODY        = 50000
HN_API          = "https://hacker-news.firebaseio.com/v0"
HN_TOP_N        = 30   # top stories to fetch
HN_COMMENT_TOP  = 10   # stories to fetch comments for (by score)
HN_COMMENT_EACH = 5    # comments per story
FEED_PATHS      = ["/feed", "/atom.xml", "/feed.xml", "/index.xml", "/rss.xml", "/rss"]
HEADERS         = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


# ==== HTML Parsers ====
class TitleExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title, self.title = False, ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class FeedLinkExtractor(HTMLParser):
    """提取HTML <head>中声明的RSS/Atom feed链接。"""
    def __init__(self, base_url):
        super().__init__()
        self.base_url, self.feed_url = base_url, None

    def handle_starttag(self, tag, attrs):
        if tag != "link" or self.feed_url:
            return
        d = dict(attrs)
        if d.get("rel") == "alternate" and d.get("type", "") in (
            "application/rss+xml", "application/atom+xml"
        ):
            href = d.get("href", "")
            if href:
                self.feed_url = urljoin(self.base_url, href)


class LinkExtractor(HTMLParser):
    """从HTML中提取文章链接(a标签)，用于scraping fallback。"""
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.articles = []
        self.current_href = None
        self.current_text = ""
        self.in_a = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.in_a, self.current_text = True, ""
            for name, val in attrs:
                if name == "href":
                    self.current_href = val

    def handle_endtag(self, tag):
        if tag == "a" and self.in_a:
            self.in_a = False
            if self.current_href and self.current_text.strip():
                href = self.current_href
                if not href.startswith("http"):
                    href = urljoin(self.base_url, href)
                title = self.current_text.strip()
                if (len(title) > 10
                        and not href.startswith("javascript:")
                        and "#" not in href.split("/")[-1][:1]):
                    self.articles.append({"title": title[:200], "link": href})
            self.current_href = None
            self.current_text = ""

    def handle_data(self, data):
        if self.in_a:
            self.current_text += data


# ==== Utilities ====
def strip_html(html_text):
    text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_parse(parser, text):
    try:
        parser.feed(text)
    except Exception:
        pass


def extract_title(html):
    te = TitleExtractor()
    _safe_parse(te, html[:5000])
    return te.title.strip()


def extract_feed_link(html, base_url):
    fe = FeedLinkExtractor(base_url)
    _safe_parse(fe, html[:10000])
    return fe.feed_url


def extract_links(html, base_url):
    le = LinkExtractor(base_url)
    _safe_parse(le, html)
    seen, out = set(), []
    for a in le.articles:
        if a["link"] not in seen:
            seen.add(a["link"])
            out.append(a)
        if len(out) >= 30:
            break
    return out


def parse_feed_xml(text):
    """解析RSS 2.0 / Atom XML，返回文章列表或None。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    articles = []

    # RSS 2.0: <rss><channel><item>
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link") or "").strip()
        desc  = item.findtext("description") or ""
        if title and link:
            a = {"title": title[:200], "link": link}
            if desc:
                a["summary"] = strip_html(desc)[:300]
            articles.append(a)

    # Atom (with namespace)
    ns = "http://www.w3.org/2005/Atom"
    if not articles:
        for entry in root.iter(f"{{{ns}}}entry"):
            title   = (entry.findtext(f"{{{ns}}}title") or "").strip()
            link_el = entry.find(f"{{{ns}}}link[@href]")
            link    = link_el.get("href", "") if link_el is not None else ""
            desc    = (entry.findtext(f"{{{ns}}}summary")
                       or entry.findtext(f"{{{ns}}}content") or "")
            if title and link:
                a = {"title": title[:200], "link": link}
                if desc:
                    a["summary"] = strip_html(desc)[:300]
                articles.append(a)

    # Atom (no namespace fallback)
    if not articles:
        for entry in root.iter("entry"):
            title   = (entry.findtext("title") or "").strip()
            link_el = entry.find("link[@href]")
            link    = link_el.get("href", "") if link_el is not None else ""
            desc    = entry.findtext("summary") or entry.findtext("content") or ""
            if title and link:
                a = {"title": title[:200], "link": link}
                if desc:
                    a["summary"] = strip_html(desc)[:300]
                articles.append(a)

    return articles[:30] if articles else None


def parse_sources(filepath):
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(">"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                urls.append(line)
    return urls


# ==== HN Handler ====
async def _hn_item(session, item_id):
    try:
        async with session.get(
            f"{HN_API}/item/{item_id}.json",
            timeout=aiohttp.ClientTimeout(total=10), ssl=False
        ) as resp:
            return await resp.json() if resp.status == 200 else {}
    except Exception:
        return {}


async def fetch_hn(session, sem, url):
    """HN专用: API抓取top stories + 精选评论。"""
    async with sem:
        try:
            async with session.get(
                f"{HN_API}/topstories.json",
                timeout=aiohttp.ClientTimeout(total=TIMEOUT), ssl=False
            ) as resp:
                sids = (await resp.json())[:HN_TOP_N]

            stories = [s for s in await asyncio.gather(
                *[_hn_item(session, sid) for sid in sids]
            ) if s.get("title")]

            by_score = sorted(stories, key=lambda s: s.get("score", 0), reverse=True)
            for story in by_score[:HN_COMMENT_TOP]:
                kids = story.get("kids", [])[:HN_COMMENT_EACH]
                if kids:
                    comments = await asyncio.gather(*[_hn_item(session, k) for k in kids])
                    story["top_comments"] = [
                        strip_html(c.get("text", ""))[:500]
                        for c in comments if c.get("text")
                    ]

            articles = []
            for s in stories:
                hn_link = f"https://news.ycombinator.com/item?id={s['id']}"
                a = {
                    "title":        s.get("title", ""),
                    "link":         s.get("url") or hn_link,
                    "hn_link":      hn_link,
                    "score":        s.get("score", 0),
                    "num_comments": s.get("descendants", 0),
                }
                if "top_comments" in s:
                    a["top_comments"] = s["top_comments"]
                articles.append(a)

            return {
                "url": url, "title": "Hacker News",
                "articles": articles, "body_snippet": "", "error": None,
            }
        except Exception as e:
            return {"url": url, "error": str(e)[:200]}


SITE_HANDLERS = {
    "news.ycombinator.com": fetch_hn,
}


# ==== Feed Fetcher ====
async def _try_parse_feed(session, feed_url):
    """Fetch + parse单个feed URL。返回文章列表或None。"""
    try:
        async with session.get(
            feed_url, timeout=aiohttp.ClientTimeout(total=15),
            headers=HEADERS, ssl=False, allow_redirects=True
        ) as resp:
            if resp.status != 200:
                return None
            text = await resp.text(errors="replace")
            return parse_feed_xml(text) if "<" in text[:200] else None
    except Exception:
        return None


async def _find_feed_articles(session, html, url):
    """Feed优先: HTML声明的feed > 常见路径并发探测。"""
    # 1. HTML中声明的feed
    feed_url = extract_feed_link(html, url)
    if feed_url:
        articles = await _try_parse_feed(session, feed_url)
        if articles:
            return articles

    # 2. 常见路径并发探测
    base = url.rstrip("/")
    results = await asyncio.gather(*[
        _try_parse_feed(session, base + p) for p in FEED_PATHS
    ])
    return next((r for r in results if r), None)


# ==== Generic Fetcher ====
async def fetch_one(session, sem, url):
    """抓取单个URL: site handler > feed > HTML scraping。"""
    domain = urlparse(url).netloc.lstrip("www.")
    handler = SITE_HANDLERS.get(domain)
    if handler:
        return await handler(session, sem, url)

    async with sem:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                headers=HEADERS, ssl=False, allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return {"url": url, "error": f"HTTP {resp.status}"}
                ct = resp.headers.get("Content-Type", "")
                if "html" not in ct and "xml" not in ct:
                    return {"url": url, "error": f"Non-HTML: {ct}"}
                html = await resp.text(errors="replace")

            title = extract_title(html) or url

            # Feed优先
            articles = await _find_feed_articles(session, html, url)
            if articles:
                return {
                    "url": url, "title": title,
                    "articles": articles, "body_snippet": "", "error": None,
                }

            # Fallback: HTML scraping
            articles = extract_links(html, url)
            body = strip_html(html)[:MAX_BODY]
            return {
                "url": url, "title": title, "articles": articles[:30],
                "body_snippet": body[:2000], "error": None,
            }

        except asyncio.TimeoutError:
            return {"url": url, "error": "Timeout"}
        except Exception as e:
            return {"url": url, "error": str(e)[:200]}


def _normalize_url(url):
    """去除trailing slash、fragment、utm_*参数，用于去重比较。"""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    p = urlparse(url.rstrip("/"))
    qs = {k: v for k, v in parse_qs(p.query).items() if not k.startswith("utm_")}
    return urlunparse((p.scheme, p.netloc, p.path, p.params,
                        urlencode(qs, doseq=True), ""))


def _dedup_articles(results):
    """跨源去重: 同一文章在多个源出现时，标记seen_in并只保留最丰富的一份。"""
    # link → (source_title, article_ref, richness_score)
    seen = {}  # normalized_link → {"best": (source, article), "sources": [source...]}

    for r in results:
        if r.get("error") or not r.get("articles"):
            continue
        src = r.get("title", r["url"])
        for a in r["articles"]:
            key = _normalize_url(a.get("link", ""))
            if not key:
                continue
            # richness: HN score/comments > feed summary > bare link
            rich = (a.get("score", 0) > 0) * 10 + bool(a.get("summary")) * 5 + 1
            if key not in seen:
                seen[key] = {"best_rich": rich, "best_src": src, "best_art": a, "sources": [src]}
            else:
                seen[key]["sources"].append(src)
                if rich > seen[key]["best_rich"]:
                    seen[key]["best_rich"] = rich
                    seen[key]["best_src"]  = src
                    seen[key]["best_art"]  = a

    # 给重复文章标记seen_in，非最佳副本标记duplicate
    dup_links = {k: v for k, v in seen.items() if len(v["sources"]) > 1}
    for r in results:
        if r.get("error") or not r.get("articles"):
            continue
        src = r.get("title", r["url"])
        kept = []
        for a in r["articles"]:
            key = _normalize_url(a.get("link", ""))
            if key in dup_links:
                info = dup_links[key]
                if src == info["best_src"] and a is info["best_art"]:
                    a["seen_in"] = info["sources"]
                    kept.append(a)
                # else: drop duplicate from non-best source
            else:
                kept.append(a)
        r["articles"] = kept


# ==== Main ====
async def main(sources_path):
    urls = parse_sources(sources_path)
    if not urls:
        print(json.dumps({"error": "No URLs found in sources.txt"}))
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, force_close=True)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_one(session, sem, url) for url in urls]
        results = await asyncio.gather(*tasks)

    # 跨源去重: 同一link出现在多个聚合站时，标记seen_in，保留信息最丰富的
    _dedup_articles(results)

    ok   = sum(1 for r in results if not r.get("error"))
    fail = sum(1 for r in results if r.get("error"))
    print(json.dumps({
        "stats": {"total": len(urls), "ok": ok, "failed": fail},
        "results": results
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <sources.txt路径>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
