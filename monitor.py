import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

BASE = "https://www.chromehearts.com"
TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE = Path("seen_products.json")

# 실제 확인된 공식 쇼핑 페이지
PAGES = [
    "/",
    "/eyewear.html",
    "/baccarat",
    "/scents",
    "/boxers-leggings",
    "/intimates",
    "/socks",
    "/hat",
    "/scarf",
    "/shop",
]

# 사이트맵 주소
SITEMAPS = [
    BASE + "/sitemap.xml",
    BASE + "/sitemap_0.xml",
]

SKIP = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms|"
    r"on/demandware)(/|$)",
    re.I,
)

CATEGORY_PATHS = {
    urlparse(urljoin(BASE, page)).path.rstrip("/") or "/"
    for page in PAGES
}


class Links(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.found = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href", "").strip()

        if not href or href.startswith(
            ("mailto:", "tel:", "javascript:", "#")
        ):
            return

        url = normalize_url(urljoin(self.page_url, href))

        if url:
            self.found.add(url)


def normalize_url(url):
    url = urldefrag(url)[0]
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    if parsed.netloc.lower() not in (
        "www.chromehearts.com",
        "chromehearts.com",
    ):
        return None

    path = parsed.path.rstrip("/") or "/"

    if SKIP.search(path):
        return None

    # 추적용 쿼리 문자열 제거
    return BASE + path


def get_response(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; "
                "ChromeHeartsProductMonitor/5.0)"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(
                f"HTTP {response.status}: {url}"
            )

        return response.read()


def get_html(url):
    data = get_response(url)
    return data.decode("utf-8", errors="replace")


def is_product_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"

    if path in CATEGORY_PATHS:
        return False

    if SKIP.search(path):
        return False

    # 공식 상품 상세 페이지로 확인할 수 있는 HTML 주소
    return path.lower().endswith(".html")


def collect_from_pages():
    products = set()
    successful = 0

    for page in PAGES:
        url = urljoin(BASE, page)
        print(f"Checking page: {url}", flush=True)

        try:
            html = get_html(url)

            parser = Links(url)
            parser.feed(html)

            found = {
                link for link in parser.found
                if is_product_url(link)
            }

            products.update(found)
            successful += 1

            print(
                f"  Links: {len(parser.found)}; "
                f"products: {len(found)}",
                flush=True,
            )

        except Exception as exc:
            print(
                f"  PAGE SKIPPED: {exc}",
                flush=True,
            )

        time.sleep(1)

    print(
        f"Successful category pages: {successful}/{len(PAGES)}",
        flush=True,
    )

    return products, successful


def collect_from_sitemap():
    products = set()
    visited = set()
    pending = list(SITEMAPS)

    sitemap_count = 0
    max_sitemaps = 20

    while pending and sitemap_count < max_sitemaps:
        sitemap_url = pending.pop(0)

        if sitemap_url in visited:
            continue

        visited.add(sitemap_url)
        sitemap_count += 1

        print(
            f"Checking sitemap: {sitemap_url}",
            flush=True,
        )

        try:
            data = get_response(sitemap_url)
            root = ET.fromstring(data)

            root_type = root.tag.split("}")[-1].lower()

            locations = [
                element.text.strip()
                for element in root.iter()
                if element.tag.split("}")[-1].lower() == "loc"
                and element.text
            ]

            if root_type == "sitemapindex":
                for location in locations:
                    if len(pending) < max_sitemaps:
                        pending.append(location)

            elif root_type == "urlset":
                for location in locations:
                    url = normalize_url(location)

                    if url and is_product_url(url):
                        products.add(url)

                print(
                    f"  Sitemap products: {len(products)}",
                    flush=True,
                )

        except Exception as exc:
            print(
                f"  SITEMAP SKIPPED: {exc}",
                flush=True,
            )

        time.sleep(1)

    print(
        f"Total sitemap product candidates: {len(products)}",
        flush=True,
    )

    return products, sitemap_count


def collect_products():
    page_products, successful_pages = collect_from_pages()
    sitemap_products, sitemap_count = collect_from_sitemap()

    products = page_products | sitemap_products

    print(
        f"Page products: {len(page_products)}",
        flush=True,
    )
    print(
        f"Sitemap products: {len(sitemap_products)}",
        flush=True,
    )
    print(
        f"Combined products: {len(products)}",
        flush=True,
    )

    if successful_pages == 0 and not products:
        raise RuntimeError(
            "All category pages failed and no products were found."
        )

    if not products:
        raise RuntimeError(
            "No product links found. "
            "Existing baseline will be protected."
        )

    return products


def extract_urls(data):
    urls = set()

    if isinstance(data, str):
        url = normalize_url(data)

        if url:
            urls.add(url)

    elif isinstance(data, list):
        for item in data:
            urls.update(extract_urls(item))

    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str):
                url = normalize_url(key)

                if url:
                    urls.add(url)

            urls.update(extract_urls(value))

    return urls


def load_seen():
    if not STATE.exists():
        return None

    try:
        data = json.loads(
            STATE.read_text(encoding="utf-8")
        )
        return extract_urls(data)

    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read {STATE}: {exc}"
        ) from exc


def save_seen(products):
    temp = STATE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            {
                "version": 5,
                "products": sorted(products),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE)


def notify(title, message):
    url = "https://ntfy.sh/" + TOPIC

    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "tada",
            "Click": BASE,
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status not in (200, 201):
            raise RuntimeError(
                f"ntfy returned HTTP {response.status}"
            )

    print("Notification sent successfully.", flush=True)


def main():
    if not TOPIC:
        raise RuntimeError("NTFY_TOPIC is empty.")

    current = collect_products()
    previous = load_seen()

    if previous is None:
        save_seen(current)

        print(
            f"Initial baseline saved: {len(current)} links",
            flush=True,
        )

        notify(
            "Chrome Hearts Monitor",
            "Monitoring started successfully.\n"
            f"Baseline: {len(current)} product links.",
        )
        return

    new_products = current - previous

    print(
        f"Previously recorded: {len(previous)}",
        flush=True,
    )
    print(
        f"Currently found: {len(current)}",
        flush=True,
    )
    print(
        f"New products: {len(new_products)}",
        flush=True,
    )

    if new_products:
        message = (
            f"New product links detected: "
            f"{len(new_products)}\n\n"
            + "\n".join(sorted(new_products))
        )

        notify(
            "Chrome Hearts: New Product",
            message,
        )

    save_seen(previous | current)

    print(
        "Product records updated successfully.",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"MONITOR ERROR: {exc}",
            flush=True,
        )
        raise
