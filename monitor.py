import json
import os
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

BASE = "https://www.chromehearts.com"
TOPIC = os.environ["NTFY_TOPIC"]
STATE = Path("seen_products.json")
VERSION = 2

# Official site sections to check.
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
        if parsed.netloc.lower() not in (
            "www.chromehearts.com", "chromehearts.com"
        ):
            return

        path = parsed.path.rstrip("/") or "/"
        if SKIP.search(path):
            return
        if path in PAGES:
            return

        self.found.add(f"https://www.chromehearts.com{path}")


def get_html(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 ChromeHeartsMonitor/2.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="replace")


def notify(url):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=f"새 링크 발견:\n{url}".encode(),
        headers={
            "Title": "Chrome Hearts 새 링크 감지",
            "Priority": "high",
            "Tags": "bell",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        if res.status >= 300:
            raise RuntimeError("ntfy 알림 전송 실패")


def main():
    current = set()
    errors = []

    for path in PAGES:
        url = urljoin(BASE + "/", path.lstrip("/"))
        try:
            parser = Links(url)
            parser.feed(get_html(url))
            current.update(parser.found)
            print(f"Checked {path}: {len(parser.found)} links")
        except Exception as exc:
            errors.append(path)
            print(f"Could not check {path}: {type(exc).__name__}")

    if not current:
        raise RuntimeError("링크를 가져오지 못했습니다. 사이트 접근을 확인하세요.")

    # Do not send alerts if this is the first run of the new version.
    old_data = {}
    if STATE.exists():
        try:
            old_data = json.loads(STATE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old_data = {}

    if not isinstance(old_data, dict) or old_data.get("version") != VERSION:
        STATE.write_text(
            json.dumps(
                {"version": VERSION, "links": sorted(current)},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"New baseline saved: {len(current)} links")
        if errors:
            print(f"Some pages could not be checked: {errors}")
        return

    previous = set(old_data.get("links", []))
    new_links = sorted(current - previous)

    for link in new_links:
        notify(link)
        print(f"Notification sent: {link}")

    STATE.write_text(
        json.dumps(
            {"version": VERSION, "links": sorted(previous | current)},
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Total links: {len(current)}; new links: {len(new_links)}")
    if errors:
        print(f"Some pages could not be checked: {errors}")


if __name__ == "__main__":
    main()
