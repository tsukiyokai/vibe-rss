#!/usr/bin/env python3
"""
vibe-rss URL fetcher: 并发抓取sources.txt中所有URL的最新内容。
输出JSON数组到stdout，供Claude消费。

用法: python3 fetch_urls.py <sources.txt路径>
"""

import asyncio
import json
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp


CONCURRENCY = 20
TIMEOUT = 45
MAX_BODY = 50000  # 每个页面最多取50KB文本


class TitleExtractor(HTMLParser):
    """从HTML中提取<title>标签内容。"""
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class LinkExtractor(HTMLParser):
    """从HTML中提取文章链接(a标签的href)和标题。"""
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.articles = []
        self.current_href = None
        self.current_text = ""
        self.in_a = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.in_a = True
            self.current_text = ""
            for name, val in attrs:
                if name == "href":
                    self.current_href = val

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.in_a:
            self.in_a = False
            if self.current_href and self.current_text.strip():
                href = self.current_href
                if not href.startswith("http"):
                    href = urljoin(self.base_url, href)
                title = self.current_text.strip()
                # 过滤导航链接、短标题、锚点
                if (len(title) > 10
                        and not href.startswith("javascript:")
                        and "#" not in href.split("/")[-1][:1]):
                    self.articles.append({
                        "title": title[:200],
                        "link": href
                    })
            self.current_href = None
            self.current_text = ""

    def handle_data(self, data):
        if self.in_a:
            self.current_text += data


def strip_html(html_text):
    """粗暴地去掉HTML标签，提取纯文本。"""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_sources(filepath):
    """解析sources.txt，返回URL列表。"""
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(">"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                urls.append(line)
    return urls


async def fetch_one(session, sem, url):
    """抓取单个URL，返回结构化结果。"""
    async with sem:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            }
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                headers=headers, ssl=False,
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return {"url": url, "error": f"HTTP {resp.status}"}

                content_type = resp.headers.get("Content-Type", "")
                if "html" not in content_type and "xml" not in content_type:
                    return {"url": url, "error": f"Non-HTML: {content_type}"}

                html = await resp.text(errors="replace")

                # 提取title
                te = TitleExtractor()
                try:
                    te.feed(html[:5000])
                except Exception:
                    pass
                title = te.title.strip() or url

                # 提取文章链接
                le = LinkExtractor(url)
                try:
                    le.feed(html)
                except Exception:
                    pass

                # 去重并限制数量
                seen = set()
                articles = []
                for a in le.articles:
                    if a["link"] not in seen:
                        seen.add(a["link"])
                        articles.append(a)
                    if len(articles) >= 30:
                        break

                # 提取正文片段
                body = strip_html(html)[:MAX_BODY]

                return {
                    "url": url,
                    "title": title,
                    "articles": articles[:30],
                    "body_snippet": body[:2000],
                    "error": None,
                }

        except asyncio.TimeoutError:
            return {"url": url, "error": "Timeout"}
        except Exception as e:
            return {"url": url, "error": str(e)[:200]}


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

    # 统计
    ok = sum(1 for r in results if not r.get("error"))
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
