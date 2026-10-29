import json
import os
import re
import time
import urllib.error
import urllib.request

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

BASE = "https://www.chromehearts.com"
TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE = Path("seen_products.json")

# 공식 미국 사이트에서 확인된 공개 쇼핑 페이지
PAGES = [
    "/",
    "/eyewear.html",
    "/baccarat",
    "/scents",
    "/boxers-leggings",
    "/intimates",
    "/socks",
]

# 상품이 아닌 페이지 제외
SKIP = re.compile(
    r"/(account|login|cart|checkout|search|contact|about|"
    r"locations|retailers|general|magazine|privacy|terms)(/|$)",
    re.I,
)

# 알려진 카테고리 주소
CATEGORY_PATHS = set(PAGES)


class Links(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.found = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        attrs = dict(attrs)
        href = attrs.get("href", "").strip()

        if not href or href.startswith(
            ("mailto:", "tel:", "javascript:", "#")
        ):
            return

        url = urldefrag(urljoin(self.page_url, href))[0]
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return

        if parsed.netloc.lower() not in (
            "www.chromehearts.com",
            "chromehearts.com",
        ):
            return

        path = parsed.path.rstrip("/") or "/"

        if SKIP.search(path):
            return

        # 쿼리 문자열을 제외하고 공식 주소로 정규화
        self.found.add(BASE + path)


def get_html(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; "
                "ChromeHeartsProductMonitor/4.0)"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Unexpected HTTP status {response.status}: {url}"
            )

        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def is_product_url(url):
    path = urlparse(url).path.rstrip("/") or "/"

    if path in CATEGORY_PATHS or SKIP.search(path):
        return False

    # 상품 상세 페이지로 보이는 HTML 주소
    if path.lower().endswith(".html"):
        return True

    # 카테고리 아래의 상세 주소도 수집
    parts = [part for part in path.split("/") if part]

    if len(parts) >= 2:
        return True

    return False


def collect_products():
    products = set()
    successful_pages = 0

    for page in PAGES:
        url = urljoin(BASE, page)

        print(f"Checking category: {url}", flush=True)

        html = get_html(url)

        parser = Links(url)
        parser.feed(html)

        links = parser.found
        successful_pages += 1

        found_here = {
            link for link in links
            if is_product_url(link)
        }

        products.update(found_here)

        print(
            f"  Links found: {len(links)}; "
            f"product candidates: {len(found_here)}",
            flush=True,
        )

        # 사이트에 부담을 주지 않도록 요청 간격 유지
        time.sleep(1)

    if successful_pages != len(PAGES):
        raise RuntimeError("Not all category pages were checked.")

    return products


def extract_urls(data):
    """기존 기록에서 URL을 최대한 복구한다."""
    urls = set()

    if isinstance(data, str):
        if data.startswith(BASE):
            urls.add(data.rstrip("/"))

    elif isinstance(data, list):
        for item in data:
            urls.update(extract_urls(item))

    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and key.startswith(BASE):
                urls.add(key.rstrip("/"))
            urls.update(extract_urls(value))

    return urls


def load_seen():
    if not STATE.exists():
        return None

    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
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
                "version": 4,
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

    # 전체 목록 수집이 끝나기 전에는 기록을 변경하지 않는다.
    current = collect_products()

    if not current:
        raise RuntimeError(
            "No product links found. "
            "Refusing to overwrite the existing baseline."
        )

    previous = load_seen()

    # 최초 실행 또는 복구할 기록이 없는 경우
    # 모든 기존 상품을 신상품으로 오인하지 않도록 기준선만 저장
    if previous is None:
        save_seen(current)
        print(
            f"Initial baseline saved: {len(current)} links",
            flush=True,
        )
        notify(
            "Chrome Hearts Monitor",
            "Monitoring started successfully. "
            f"Baseline: {len(current)} product links.\n"
            "Newly discovered links will trigger alerts.",
        )
        return

    new_products = current - previous

    print(f"Previously recorded: {len(previous)}", flush=True)
    print(f"Currently found: {len(current)}", flush=True)
    print(f"New links: {len(new_products)}", flush=True)

    # 새 링크가 있는 경우 알림이 성공한 뒤 기록을 저장한다.
    # 알림이 실패하면 다음 실행에서 다시 감지할 수 있다.
    if new_products:
        message = (
            f"New product links detected: {len(new_products)}\n\n"
            + "\n".join(sorted(new_products))
        )

        notify("Chrome Hearts: New Product", message)

    # 목록 수집이 성공적으로 끝난 뒤 기록 갱신
    save_seen(previous | current)

    print("Product records updated successfully.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MONITOR ERROR: {exc}", flush=True)
        raise
