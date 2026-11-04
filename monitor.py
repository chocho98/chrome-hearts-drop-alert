import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from pathlib import Path


# ==========================================
# 1. 기본 설정
# ==========================================

BASE = "https://www.chromehearts.com"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE = Path("seen_products.json")
VERSION = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Keep the storefront session across requests. Some Search-Show category
# endpoints require cookies issued by the homepage/session bootstrap.
COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR)
)

SITEMAPS = [
    BASE + "/sitemap_index.xml",
    BASE + "/sitemap.xml",
    BASE + "/sitemap_0.xml",
]

SWEATPANTS_PAGE = (
    BASE
    + "/on/demandware.store/Sites-ChromeHearts-Site/"
    + "en_US/Search-Show?cgid=SWEATPANTS"
)

# Search-Show is the native storefront search route. Probe apparel terms
# directly instead of guessing category slugs. Only real product links found
# in a non-homepage search response are eligible for monitoring.
APPAREL_SEARCH_TERMS = [
    "shirt", "t-shirt", "long sleeve", "hoodie",
    "sweatshirt", "sweatpants", "pants", "shorts", "jacket",
]

# Official category slug candidates. Empty categories may redirect home.
CATEGORY_SLUGS = [
    "socks", "scents", "baccarat", "intimates", "boxers-leggings",
    "hat", "eyewear", "sunglasses", "eyewear-accessories",
    "bag", "bags", "belt", "earring",
    "t-shirt", "t-shirts", "shirt", "shirts",
    "hoodie", "hoodies", "sweatshirt", "sweatshirts", "sweater",
    "jacket", "denim", "pants", "shorts",
    "shoes", "boots", "slippers", "home", "gloves", "scarf", "tie",
]

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
] + [f"/{slug}" for slug in CATEGORY_SLUGS]

CATEGORY_PATHS = {
    urllib.parse.urlsplit(BASE + page).path.rstrip("/") or "/"
    for page in PAGES
}

SKIP_RE = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms|disclosure)"
    r"(?:\.html)?(?:/|$)",
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


def collect_product_metadata(html, current_url):
    """
    Extract product IDs from server-rendered product tiles.
    If a category endpoint redirects directly to a product detail page,
    use that final product URL when its PID matches the metadata.
    """
    products = set()
    spans = re.findall(
        r"""<span\b(?=[^>]*\bclass=["'][^"']*product-metadata[^"']*["'])[^>]*>""",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    current_path = urllib.parse.urlsplit(current_url).path

    for span in spans:
        attrs = {
            key.lower(): value
            for key, value in re.findall(
                r"""data-([a-z]+)=["']([^"']*)["']""",
                span,
                flags=re.IGNORECASE,
            )
        }
        pid = attrs.get("pid", "").strip()
        if not pid:
            continue

        link_pattern = (
            r"""href=["']([^"']*/"""
            + re.escape(pid)
            + r""".html(?:\?[^"']*)?)["']"""
        )
        match = re.search(link_pattern, html, flags=re.IGNORECASE)

        candidate = None
        if match:
            candidate = normalize_url(match.group(1), current_url)
        elif current_path.lower().endswith("/" + pid.lower() + ".html"):
            candidate = normalize_url(current_url)

        if candidate and is_product_url(candidate):
            products.add(candidate)

    return products


def discover_category_pages(html, current_url):
    """
    Discover category pages and Search-Show category IDs from same-site links.
    """
    discovered = set()
    utility = {
        "account", "bag", "cart", "checkout", "contact", "disclosure",
        "general", "locations", "login", "magazine", "privacy", "search",
        "shop", "terms", "wishlist",
    }

    for href in re.findall(
        r"""href=["']([^"']+)["']""",
        html,
        flags=re.IGNORECASE,
    ):
        full = urllib.parse.urljoin(current_url, href)
        parsed = urllib.parse.urlsplit(full)

        if parsed.netloc.lower() not in ("www.chromehearts.com", "chromehearts.com"):
            continue

        if parsed.path.endswith("/Search-Show"):
            cgid = urllib.parse.parse_qs(parsed.query).get("cgid", [])
            if len(cgid) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", cgid[0]):
                discovered.add(
                    BASE + parsed.path + "?cgid=" + urllib.parse.quote(cgid[0], safe="")
                )
            continue

        path = parsed.path.strip("/")
        if not path or "/" in path or path.lower().endswith(".html"):
            continue

        slug = path.lower()
        if slug in utility or not re.fullmatch(r"[a-z0-9-]+", slug):
            continue

        discovered.add(BASE + "/" + path)

    return discovered


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

            cgid = query.get("cgid", [""])[0]
            if cgid and re.fullmatch(r"[A-Za-z0-9_-]+", cgid):
                return BASE + path + "?cgid=" + urllib.parse.quote(cgid, safe="")

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

    with OPENER.open(request, timeout=30) as response:
        return response.read()


def get_html(url):

    request = urllib.request.Request(
        url,
        headers=HEADERS,
    )

    with OPENER.open(request, timeout=30) as response:
        final_url = response.geturl()
        status = response.getcode()
        content_type = response.headers.get("Content-Type", "")
        content_length = response.headers.get("Content-Length", "")
        set_cookie_names = []
        for cookie_header in response.headers.get_all("Set-Cookie", []) or []:
            cookie_name = cookie_header.split("=", 1)[0].strip()
            if cookie_name:
                set_cookie_names.append(cookie_name)
        raw = response.read()
        html = raw.decode(
            "utf-8",
            errors="replace",
        )

    # Diagnostic only: expose the Search-Show response characteristics without
    # printing cookie values or changing the existing collection behavior.
    if "Search-Show" in url:
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        page_title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else "(no title)"
        metadata_count = len(re.findall(
            r"""<span\b(?=[^>]*\bclass=["'][^"']*product-metadata[^"']*["'])[^>]*>""",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        ))
        print(
            f"  SEARCH RESPONSE: status={status}; landed={final_url}; "
            f"type={content_type}; bytes={len(raw)}; "
            f"content-length={content_length or 'unknown'}; "
            f"title={page_title[:120]!r}; metadata={metadata_count}; "
            f"set-cookie-names={','.join(set_cookie_names) or 'none'}",
            flush=True,
        )

    return html, final_url


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

                    if (
                        path in CATEGORY_PATHS
                        or (
                            path != "/"
                            and "/" not in path.strip("/")
                            and not path.lower().endswith(".html")
                            and not SKIP_RE.search(path)
                        )
                    ):
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
    pending = list(sorted(pages))
    visited = set()
    discovered_total = 0
    max_pages = 100

    while pending and len(visited) < max_pages:

        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)

        print(
            f"Checking page: {url}",
            flush=True,
        )

        try:
            html, final_url = get_html(url)
            landed = urllib.parse.urlsplit(final_url)
            landed_path = landed.path.rstrip("/") or "/"

            # Empty or unavailable categories commonly redirect to homepage.
            # Do not discard redirects to actual product/category pages.
            if landed_path == "/" and urllib.parse.urlsplit(url).path.rstrip("/") != "":
                print(
                    f"  REDIRECT SKIPPED: landed on {final_url}",
                    flush=True,
                )
                time.sleep(1)
                continue

            parser = Links(final_url)
            parser.feed(html)

            found = {
                link
                for link in parser.found
                if is_product_url(link)
            }
            metadata_products = collect_product_metadata(html, final_url)
            found.update(metadata_products)
            products.update(found)

            category_candidates = discover_category_pages(html, final_url)
            newly_queued = []
            for category_url in sorted(category_candidates):
                if category_url not in visited and category_url not in pending:
                    pending.append(category_url)
                    newly_queued.append(category_url)
                    discovered_total += 1

            successful += 1

            print(
                f"  Landed: {final_url}; links: {len(parser.found)}; "
                f"metadata: {len(metadata_products)}; products: {len(found)}; "
                f"category candidates: {len(category_candidates)}; "
                f"newly queued: {len(newly_queued)}",
                flush=True,
            )
            for category_url in newly_queued:
                print(f"    DISCOVERED CATEGORY: {category_url}", flush=True)

        except Exception as exc:
            print(
                f"  PAGE SKIPPED: {exc}",
                flush=True,
            )

        time.sleep(1)

    if pending:
        print(
            f"WARNING: Page limit reached; {len(pending)} discovered pages remain.",
            flush=True,
        )

    print(
        f"Successful pages: {successful}/{len(visited)}; "
        f"discovered category links: {discovered_total}",
        flush=True,
    )

    return products, successful


# ==========================================
# 7. 공홈 상품 검색 결과 검사
# ==========================================

def collect_from_apparel_searches():

    products = set()
    successful_searches = 0

    for term in APPAREL_SEARCH_TERMS:
        query = urllib.parse.urlencode({"q": term})
        url = (
            BASE
            + "/on/demandware.store/Sites-ChromeHearts-Site/"
            + "en_US/Search-Show?"
            + query
        )

        print(f"Checking apparel search: {term}", flush=True)

        try:
            html, final_url = get_html(url)
            landed_path = urllib.parse.urlsplit(final_url).path.rstrip("/") or "/"

            # A homepage redirect means the storefront did not provide results.
            if landed_path == "/":
                print(f"  SEARCH REDIRECTED: {final_url}", flush=True)
                time.sleep(1)
                continue

            parser = Links(final_url)
            parser.feed(html)

            found = {
                link for link in parser.found
                if is_product_url(link)
            }
            metadata_products = collect_product_metadata(html, final_url)
            found.update(metadata_products)

            # Do not count a generic page as a successful search response.
            if found or "Search-Show" in final_url:
                successful_searches += 1

            products.update(found)
            print(
                f"  Landed: {final_url}; links: {len(parser.found)}; "
                f"metadata: {len(metadata_products)}; products: {len(found)}",
                flush=True,
            )

        except Exception as exc:
            print(f"  SEARCH SKIPPED ({term}): {exc}", flush=True)

        time.sleep(1)

    print(
        f"Apparel searches successful: {successful_searches}/"
        f"{len(APPAREL_SEARCH_TERMS)}; products: {len(products)}",
        flush=True,
    )
    return products


# ==========================================
# 8. 전체 상품 수집
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

    apparel_search_products = collect_from_apparel_searches()
    print(
        f"Apparel search products: {len(apparel_search_products)}",
        flush=True,
    )

    print(
        f"Sitemap products: {len(sitemap_products)}",
        flush=True,
    )

    combined = page_products | sitemap_products | apparel_search_products

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
