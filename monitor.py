import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from pathlib import Path


# ==========================================
# 1. 기본 설정
# ==========================================

BASE = "https://www.chromehearts.com"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE = Path("seen_products.json")
VERSION = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SITEMAPS = [
    BASE + "/sitemap.xml",
    BASE + "/sitemap_0.xml",
]

SWEATPANTS_PAGE = (
    BASE
    + "/on/demandware.store/Sites-ChromeHearts-Site/"
    + "en_US/Search-Show?cgid=SWEATPANTS"
)

PAGES = [
    "/",
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
    "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Default-Start",
    "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=SWEATPANTS",
]

CATEGORY_PATHS = {
    urllib.parse.urlsplit(BASE + page).path.rstrip("/") or "/"
    for page in PAGES
}

SKIP_RE = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms)(/|$)",
    re.IGNORECASE,
)


# ==========================================
# 2. HTML 링크 수집
# ==========================================

class Links(HTMLParser):

    def __init__(self, page_url):
        super().__init__()
        self.page_url = page_url
        self.found = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        for key, value in attrs:
            if key.lower() == "href" and value:
                normalized = normalize_url(value, self.page_url)

                if normalized:
                    self.found.add(normalized)


# ==========================================
# 3. URL 정리 및 상품 판별
# ==========================================

def normalize_url(href, current_url=None):

    if not href:
        return None

    try:
        base_url = current_url or BASE + "/"
        full_url = urllib.parse.urljoin(base_url, href)

        parsed = urllib.parse.urlsplit(full_url)

        if parsed.netloc.lower() not in (
            "www.chromehearts.com",
            "chromehearts.com",
        ):
            return None

        path = parsed.path.rstrip("/") or "/"

        # 스웨트팬츠 검색 주소는 검색 조건을 보존
        if path.endswith("/Search-Show"):
            query = urllib.parse.parse_qs(parsed.query)

            if query.get("cgid", [""])[0].upper() == "SWEATPANTS":
                return (
                    BASE
                    + path
                    + "?cgid=SWEATPANTS"
                )

        return BASE + path

    except Exception:
        return None


def is_product_url(url):

    try:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path

        if path.rstrip("/") in CATEGORY_PATHS:
            return False

        if SKIP_RE.search(path):
            return False

        # 상품 상세 페이지 후보
        return path.lower().endswith(".html")

    except Exception:
        return False


# ==========================================
# 4. 웹페이지 가져오기
# ==========================================

def get_response(url):

    request = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def get_html(url):

    data = get_response(url)

    return data.decode(
        "utf-8",
        errors="replace",
    )


# ==========================================
# 5. 사이트맵 확인
# ==========================================

def collect_from_sitemaps():

    sitemap_pages = set()
    sitemap_products = set()

    pending = list(SITEMAPS)
    visited = set()

    while pending:

        sitemap_url = pending.pop(0)

        if sitemap_url in visited:
            continue

        visited.add(sitemap_url)

        print(
            f"Checking sitemap: {sitemap_url}",
            flush=True,
        )

        try:
            data = get_response(sitemap_url)
            root = ET.fromstring(data)

            root_name = root.tag.split("}")[-1].lower()

            if root_name == "sitemapindex":

                for element in root.iter():
                    if element.tag.split("}")[-1] == "loc":
                        if element.text:
                            pending.append(element.text.strip())

                continue

            if root_name == "urlset":

                for element in root.iter():
                    if element.tag.split("}")[-1] != "loc":
                        continue

                    if not element.text:
                        continue

                    url = normalize_url(element.text.strip())

                    if not url:
                        continue

                    path = urllib.parse.urlsplit(url).path.rstrip("/") or "/"

                    if path in CATEGORY_PATHS:
                        sitemap_pages.add(url)

                    elif is_product_url(url):
                        sitemap_products.add(url)

        except Exception as exc:

            print(
                f"  SITEMAP SKIPPED: {exc}",
                flush=True,
            )

    print(
        f"  Category pages: {len(sitemap_pages)}; "
        f"products: {len(sitemap_products)}",
        flush=True,
    )

    return sitemap_pages, sitemap_products


# ==========================================
# 6. 카테고리 페이지 검사
# ==========================================

def collect_from_pages(pages):

    products = set()
    successful = 0

    for url in sorted(pages):

        print(
            f"Checking page: {url}",
            flush=True,
        )

        try:
            html = get_html(url)

            parser = Links(url)
            parser.feed(html)

            # 스웨트팬츠 페이지의 링크를 별도로 출력
            if "Search-Show" in url:

                print(
                    "=== SWEATPANTS PAGE LINKS ===",
                    flush=True,
                )

                for link in sorted(parser.found):
                    print(
                        f"SEARCH LINK: {link}",
                        flush=True,
                    )

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


# ==========================================
# 7. 전체 상품 수집
# ==========================================

def collect_products():

    sitemap_pages, sitemap_products = collect_from_sitemaps()

    pages = set(sitemap_pages)

    for page in PAGES:
        normalized = normalize_url(page)

        if normalized:
            pages.add(normalized)

    print(
        f"Total pages to inspect: {len(pages)}",
        flush=True,
    )

    page_products, successful = collect_from_pages(pages)

    print(
        f"Page products: {len(page_products)}",
        flush=True,
    )

    print(
        f"Sitemap products: {len(sitemap_products)}",
        flush=True,
    )

    combined = page_products | sitemap_products

    print(
        f"Combined products: {len(combined)}",
        flush=True,
    )

    minimum_success = min(3, len(pages))

    if successful < minimum_success:
        raise RuntimeError(
            "Too few pages were checked successfully. "
            "Previous records were not changed."
        )

    if not combined:
        raise RuntimeError(
            "No products found. "
            "Previous records were not changed."
        )

    return combined


# ==========================================
# 8. 기존 상품 기록 불러오기
# ==========================================

def load_seen():

    if not STATE.exists():
        return set()

    try:
        with STATE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        # 기존 버전의 저장 형식도 지원
        if isinstance(data, list):
            return set(data)

        if isinstance(data, dict):

            for key in (
                "products",
                "seen_products",
                "urls",
            ):
                if isinstance(data.get(key), list):
                    return set(data[key])

        return set()

    except Exception as exc:

        print(
            f"WARNING: Could not read previous records: {exc}",
            flush=True,
        )

        raise RuntimeError(
            "Previous product records could not be read. "
            "Stopping to protect existing data."
        )


# ==========================================
# 9. 상품 기록 저장
# ==========================================

def save_seen(products):

    data = {
        "version": VERSION,
        "products": sorted(products),
    }

    temp_file = STATE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temp_file.replace(STATE)


# ==========================================
# 10. ntfy 알림 전송
# ==========================================

def notify(new_products):

    if not NTFY_TOPIC:
        raise RuntimeError(
            "NTFY_TOPIC is missing. Check GitHub Secrets."
        )

    topic = urllib.parse.quote(
        NTFY_TOPIC,
        safe="",
    )

    url = f"https://ntfy.sh/{topic}"

    message = (
        "Chrome Hearts 신규 상품 발견!\n\n"
        + "\n".join(sorted(new_products))
    )

    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": "Chrome Hearts 신상품 알림",
            "Priority": "high",
            "Tags": "shopping",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        print(
            f"Notification sent: HTTP {response.status}",
            flush=True,
        )


# ==========================================
# 11. 실행
# ==========================================

def main():

    print("Chrome Hearts Monitor started", flush=True)

    previous = load_seen()

    print(
        f"Previously recorded: {len(previous)}",
        flush=True,
    )

    current = collect_products()

    print(
        f"Currently found: {len(current)}",
        flush=True,
    )

    # 처음 실행하는 경우 기준 데이터만 저장
    if not previous:

        save_seen(current)

        print(
            f"Initial baseline saved: {len(current)} links",
            flush=True,
        )

        return

    new_products = current - previous

    print(
        f"New products: {len(new_products)}",
        flush=True,
    )

    if new_products:

        for product in sorted(new_products):
            print(
                f"NEW PRODUCT: {product}",
                flush=True,
            )

        notify(new_products)

    else:

        print(
            "No new products found.",
            flush=True,
        )

    # 기존 기록은 유지하고 새로 발견한 주소 추가
    save_seen(previous | current)

    print(
        "Product records updated successfully.",
        flush=True,
    )


if __name__ == "__main__":
    main()
