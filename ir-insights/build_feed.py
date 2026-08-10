#!/usr/bin/env python3
import csv
import html
import json
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

OUT_DIR = Path(__file__).resolve().parent
FEED_PATH = OUT_DIR / "feed.xml"
CSV_PATH = OUT_DIR / "inventory.csv"
REPORT_PATH = OUT_DIR / "build_report.txt"

BASES = [
    "https://gsekretaryuk2000.wixsite.com/ir-insights",
    "https://www.irinsightsblog.com",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IRInsightsMigration/1.0; +https://github.com/LordGenya/LordGenya)"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch(url, timeout=30):
    r = SESSION.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r


def normalize_url(url):
    u = url.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")


def collect_sitemap_urls(url, seen=None):
    seen = seen or set()
    url = normalize_url(url)
    if url in seen:
        return set()
    seen.add(url)
    try:
        r = fetch(url)
    except Exception:
        return set()
    text = r.text
    try:
        root = etree.fromstring(text.encode("utf-8", "ignore"))
    except Exception:
        return set()
    locs = []
    for el in root.xpath("//*[local-name()='loc']"):
        if el.text:
            locs.append(el.text.strip())
    urls = set()
    if root.tag.lower().endswith("sitemapindex"):
        for loc in locs:
            urls |= collect_sitemap_urls(loc, seen)
    else:
        urls |= {normalize_url(x) for x in locs}
    return urls


def crawl_index(base):
    found = set()
    for path in ["/blog", "/", "/post"]:
        try:
            r = fetch(base.rstrip("/") + path)
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = normalize_url(urljoin(r.url, a["href"]))
            if "/post/" in href:
                found.add(href)
    return found


def find_blogposting(obj):
    if isinstance(obj, dict):
        typ = obj.get("@type")
        if typ == "BlogPosting" or (isinstance(typ, list) and "BlogPosting" in typ):
            return obj
        if "@graph" in obj:
            hit = find_blogposting(obj["@graph"])
            if hit:
                return hit
        for v in obj.values():
            hit = find_blogposting(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = find_blogposting(item)
            if hit:
                return hit
    return None


def extract_jsonld(soup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text("", strip=False)
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        hit = find_blogposting(obj)
        if hit:
            return hit
    return {}


def author_name(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("givenName") or ""
    if isinstance(value, list):
        names = [author_name(x) for x in value]
        return ", ".join(x for x in names if x)
    return str(value or "")


def image_url(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or ""
    if isinstance(value, list) and value:
        return image_url(value[0])
    return ""


def clean_container(container, page_url):
    if container is None:
        return ""
    clone = BeautifulSoup(str(container), "lxml")
    body = clone.body or clone
    for bad in body.find_all(["script", "style", "button", "form", "noscript", "svg"]):
        bad.decompose()
    # Remove common Wix chrome / engagement widgets.
    for el in list(body.find_all(True)):
        attrs = " ".join([str(el.get("class", "")), str(el.get("data-hook", "")), str(el.get("aria-label", ""))]).lower()
        if any(k in attrs for k in ["comment", "like-button", "share", "social", "reaction", "login", "follow"]):
            if el.name not in ["p", "h1", "h2", "h3", "h4", "blockquote", "img", "a"]:
                el.decompose()
    for img in body.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-pin-media")
        if src:
            img["src"] = urljoin(page_url, src)
        for attr in ["srcset", "data-src", "data-hook", "class", "style", "loading"]:
            img.attrs.pop(attr, None)
    for a in body.find_all("a", href=True):
        a["href"] = urljoin(page_url, a["href"])
        a.attrs.pop("class", None)
        a.attrs.pop("style", None)
    for el in body.find_all(True):
        for attr in ["class", "style", "id", "data-hook", "data-testid", "dir"]:
            el.attrs.pop(attr, None)
    inner = "".join(str(x) for x in body.contents).strip()
    return inner


def plain_to_html(text):
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n", text)
    out = []
    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if not lines:
            continue
        escaped = "<br/>".join(html.escape(x) for x in lines)
        out.append(f"<p>{escaped}</p>")
    return "\n".join(out)


def extract_article(url):
    r = fetch(url)
    canonical = normalize_url(r.url)
    soup = BeautifulSoup(r.text, "lxml")
    data = extract_jsonld(soup)

    title = (data.get("headline") or data.get("name") or "").strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else canonical.rsplit("/", 1)[-1]

    author = author_name(data.get("author")).strip()
    date = (data.get("datePublished") or data.get("dateCreated") or "").strip()
    desc = (data.get("description") or "").strip()
    hero = image_url(data.get("image")).strip()

    article_body = data.get("articleBody") or ""
    body_html = ""
    if article_body:
        if "<p" in article_body or "<h" in article_body or "<div" in article_body:
            body_html = clean_container(BeautifulSoup(article_body, "lxml"), canonical)
        else:
            body_html = plain_to_html(article_body)

    if not body_html or len(BeautifulSoup(body_html, "lxml").get_text(" ", strip=True)) < 200:
        selectors = [
            "[data-hook='post-content']",
            "[data-testid='post-content']",
            "article",
            ".post-content",
            ".blog-post-page",
        ]
        best = None
        best_len = 0
        for sel in selectors:
            for node in soup.select(sel):
                text_len = len(node.get_text(" ", strip=True))
                if text_len > best_len:
                    best = node
                    best_len = text_len
        if best is not None and best_len >= 200:
            candidate = clean_container(best, canonical)
            if len(BeautifulSoup(candidate, "lxml").get_text(" ", strip=True)) > len(BeautifulSoup(body_html, "lxml").get_text(" ", strip=True)):
                body_html = candidate

    # Last-resort heuristic: take the largest content-like div that contains the title.
    if not body_html or len(BeautifulSoup(body_html, "lxml").get_text(" ", strip=True)) < 200:
        best = None
        best_len = 0
        for node in soup.find_all(["main", "section", "div"]):
            txt = node.get_text(" ", strip=True)
            if title and title[:30].lower() in txt.lower() and 300 < len(txt) < 100000:
                if len(txt) > best_len:
                    best = node
                    best_len = len(txt)
        if best is not None:
            body_html = clean_container(best, canonical)

    text_len = len(BeautifulSoup(body_html, "lxml").get_text(" ", strip=True))
    if hero and body_html and hero not in body_html:
        body_html = f'<p><img src="{html.escape(hero, quote=True)}" alt=""/></p>\n' + body_html

    return {
        "title": title,
        "author": author,
        "date": date,
        "description": desc,
        "url": canonical,
        "body_html": body_html,
        "text_len": text_len,
    }


def parse_date(value):
    if not value:
        return datetime.now(timezone.utc)
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in ["%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return datetime.now(timezone.utc)


def build_feed(posts):
    NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
    NS_DC = "http://purl.org/dc/elements/1.1/"
    etree.register_namespace("content", NS_CONTENT)
    etree.register_namespace("dc", NS_DC)

    rss = etree.Element("rss", version="2.0", nsmap={"content": NS_CONTENT, "dc": NS_DC})
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = "IR Insights Blog archive"
    etree.SubElement(channel, "link").text = "https://www.irinsightsblog.com/"
    etree.SubElement(channel, "description").text = "Full-content migration feed for the IR Insights Blog archive."
    etree.SubElement(channel, "language").text = "en"
    etree.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for p in sorted(posts, key=lambda x: parse_date(x["date"]), reverse=True):
        item = etree.SubElement(channel, "item")
        etree.SubElement(item, "title").text = p["title"]
        etree.SubElement(item, "link").text = p["url"]
        guid = etree.SubElement(item, "guid", isPermaLink="true")
        guid.text = p["url"]
        etree.SubElement(item, "pubDate").text = format_datetime(parse_date(p["date"]))
        etree.SubElement(item, f"{{{NS_DC}}}creator").text = p["author"] or "IR Insights Blog"
        etree.SubElement(item, "description").text = p["description"] or BeautifulSoup(p["body_html"], "lxml").get_text(" ", strip=True)[:400]
        content = etree.SubElement(item, f"{{{NS_CONTENT}}}encoded")
        content.text = etree.CDATA(p["body_html"])

    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8", pretty_print=True).decode("utf-8")


def main():
    discovered = set()
    logs = []
    for base in BASES:
        for sm in [base.rstrip("/") + "/sitemap.xml", base.rstrip("/") + "/sitemap-index.xml"]:
            urls = collect_sitemap_urls(sm)
            if urls:
                logs.append(f"Sitemap {sm}: {len(urls)} URLs")
            discovered |= {u for u in urls if "/post/" in u}
        crawled = crawl_index(base)
        logs.append(f"Index crawl {base}: {len(crawled)} post URLs")
        discovered |= crawled

    # Prefer canonical custom-domain URLs when duplicate Wix/custom-domain paths exist.
    by_path = {}
    for u in discovered:
        path = urlparse(u).path
        if "/post/" not in path:
            continue
        prev = by_path.get(path)
        if prev is None or "irinsightsblog.com" in u:
            by_path[path] = u
    urls = sorted(by_path.values())
    logs.append(f"Unique post paths discovered: {len(urls)}")

    posts = []
    failures = []
    seen_titles = set()
    for i, url in enumerate(urls, 1):
        try:
            p = extract_article(url)
            key = p["title"].strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            posts.append(p)
            logs.append(f"[{i}/{len(urls)}] OK {p['title']} | {p['author']} | {p['date']} | {p['text_len']} chars")
        except Exception as e:
            failures.append((url, repr(e)))
            logs.append(f"[{i}/{len(urls)}] FAIL {url}: {e!r}")

    if not posts:
        REPORT_PATH.write_text("\n".join(logs + ["No posts extracted."]), encoding="utf-8")
        raise SystemExit("No posts extracted")

    FEED_PATH.write_text(build_feed(posts), encoding="utf-8")
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "author", "date", "source_url", "body_text_chars"])
        for p in sorted(posts, key=lambda x: parse_date(x["date"]), reverse=True):
            w.writerow([p["title"], p["author"], p["date"], p["url"], p["text_len"]])

    logs.append("")
    logs.append(f"Extracted posts: {len(posts)}")
    logs.append(f"Failures: {len(failures)}")
    if failures:
        logs.append("Failure details:")
        logs.extend(f"- {u}: {e}" for u, e in failures)
    REPORT_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")

    # Guard against silently publishing an obviously incomplete migration.
    short = [p for p in posts if p["text_len"] < 300]
    if short:
        print("WARNING: short extracted bodies:", file=sys.stderr)
        for p in short:
            print(f"- {p['title']}: {p['text_len']} chars", file=sys.stderr)
    print(f"Built {FEED_PATH} with {len(posts)} posts")


if __name__ == "__main__":
    main()
