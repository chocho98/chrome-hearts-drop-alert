import json
import os
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

BASE = "https://www.chromehearts.com"
TOPIC = os.environ["NTFY_TOPIC"]
STATE = Path("seen_products.json")
VERSION = 3

# Official shopping pages confirmed to be linked from the US site.
# The public navigation currently exposes these categories; do not guess hidden URLs.
PAGES = [
    "/",
    "/eyewear.html",
    "/baccarat",
    "/scents",
    "/boxers-leggings",
    "/intimates",
    "/socks",
]

SKIP = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms)(/|$)",
    re.I,
)
PRODUCT_PATH = re.compile(r".+/.+\.html$", re.I)
STOCK_PATTERNS = [
    (re.compile(r"\bOUT OF STOCK\b", re.I), "out of stock"),
    (re.compile(r"\bSOLD OUT\b", re.I), "sold out"),
    (re.compile(r"\bADD TO BAG\b", re.I), "available/add to bag"),
]


class Links(HTMLParser):
    def __init__(self, page_url):
        super().__init__()
        self.page_url = page_url
        self.found = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            return
        url = urldefrag(urljoin(self.page_url, href))[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return
        if parsed.netloc.lower() not in ("www.chromehearts.com", "chromehearts.com"):
            return
        path = parsed.path.rstrip("/") or "/"
        if SKIP.search(path) or path in PAGES:
            return
        self.found.add(f"https://www.chromehearts.com{path}")


class PageText(HTMLParser):
    """Collect visible-ish text and page title without external dependencies."""
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title_parts = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        value = " ".join(data.split())
        if not value:
            return
        self.text_parts.append(value)
        if self.in_title:
            self.title_parts.append(value)


def get_html(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ProductAvailabilityMonitor/3.0)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
