import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import (
    urljoin, urlparse, urldefrag, parse_qs
)

BASE = "https://www.chromehearts.com"
TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE = Path("seen_products.json")

# 공식 사이트에서 확인한 카테고리 및 검색 페이지
PAGES = [
    "/",
    "/Default-Start",
    "/eyewear.html",
    "/baccarat",
    "/scents",
    "/boxers-leggings",
    "/intimates",
    "/underwear",
    "/socks",
    "/hat",
    "/scarf",
    "/shop",
    "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=SWEATPANTS",
]

SITEMAPS = [
    BASE + "/sitemap.xml",
    BASE + "/sitemap_0.xml",
]

SKIP = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms)(/|$)",
    re.I,
)

CATEGORY_PATHS = {
    urlparse(urljoin(BASE, page)).path.rstrip("/") or "/"
    for page in PAGES
}


def normalize_url(url):
    """공식 도메인 URL 정리. SWEATPANTS 검색 조건은 보존한다."""
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

    # Demandware 검색 페이지는 SWEATPANTS 조건을 유지
    if path.endswith("/Search-Show"):
        params = parse_qs(parsed.query)
        if "cgid" not in params:
            return None
        if params["cgid"][0].upper() != "SWEATPANTS":
            return None
        return BASE + path + "?cgid=SWEATPANTS"

    # 그 외 URL은 추적용 쿼리 제거
    return BASE + path


def is_product_url(url):
    """상품 상세 페이지 후보만 선별한다."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"

    if path in CATEGORY_PATHS:
        return False

    if SKIP.search(path):
        return False

    if path.endswith("/Search-Show"):
        return False

    # 현재 확인된 상품 상세 URL 형식
    return path.lower().endswith(".html")


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


def get_response(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; "
                "ChromeHeartsProductMonitor/6.0)"
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
    return get_response(url).decode(
        "utf-8",
        errors="replace",
    )


def collect_from_sitemaps():
    """사이트맵에서 카테고리와 상품 후보 주소를 수집한다."""
    category_pages = set()
    products = set()
    visited = set()
    pending = list(SITEMAPS)

    while pending and len(visited) < 30:
        sitemap_url = pending.pop(0)

        if sitemap_url in visited:
            continue

        visited.add(sitemap_url)

        print(
            f"Checking sitemap: {sitemap_url}",
            flush=True,
        )

        try:
            root = ET.fromstring(get_response(sitemap_url))

            root_type = root.tag.split("}")[-1].lower()

            locations = [
                element.text.strip()
                for element in root.iter()
                if element.tag.split("}")[-1].lower() == "loc"
                and element.text
            ]

            if root_type == "sitemapindex":
                pending.extend(locations)

            elif root_type == "urlset":
                for location in locations:
                    url = normalize_url(location)

                    if not url:
                        continue

                    if is_product_url(url):
                        products.add(url)
                    else:
                        category_pages.add(url)

                print(
                    f"  Category pages: {len(category_pages)}; "
                    f"products: {len(products)}",
                    flush=True,
                )

        except Exception as exc:
            print(
                f"  SITEMAP SKIPPED: {exc}",
                flush=True,
            )

        time.sleep(1)

    return category_pages, products


def collect_from_pages(pages):
    products = set()
    successful = 0

    for url in sorted(pages):
        print(f"Checking page: {url}", flush=True)

        try:
            html = get_html(url)

        parser = Links(url)
            parser.feed(html)

            if "Search-Show" in url:
                print("=== SWEATPANTS PAGE LINKS ===", flush=True)
                for link in sorted(parser.found):
                    print(f"SEARCH LINK: {link}", flush=True)

            found = {
                link
                for link in parser.found
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
        f"Successful pages: {successful}/{len(pages)}",
        flush=True,
    )

    return products, successful


def collect_products():
    sitemap_pages, sitemap_products = collect_from_sitemaps()

    configured_pages = {
        normalize_url(urljoin(BASE, page))
        for page in PAGES
    }

    pages = {
        url for url in configured_pages | sitemap_pages
        if url
    }

    print(
        f"Total pages to inspect: {len(pages)}",
        flush=True,
    )

    page_products, successful = collect_from_pages(pages)

    # 충분한 페이지를 검사하지 못했다면 기존 기록 보호
    minimum_success = min(3, len(pages))

    if successful < minimum_success:
        raise RuntimeError(
            "Too few pages were checked successfully. "
            "Existing baseline protected."
        )

    products = page_products | sitemap_products

    if not products:
        raise RuntimeError(
            "No product links found. "
            "Existing baseline protected."
        )

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
                "version": 6,
                "products": sorted(products),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE)


def notify(title, message):
    req = urllib.request.Request(
        "https://ntfy.sh/" + TOPIC,
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

    print(
        "Notification sent successfully.",
        flush=True,
    )


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
