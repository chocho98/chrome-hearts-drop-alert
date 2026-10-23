import json
import os
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE_URL = "https://www.chromehearts.com/"
STATE_FILE = Path("seen_products.json")
TOPIC = os.environ["NTFY_TOPIC"]


class ProductLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        href = attrs.get("href", "")
        if not href:
            return

        full_url = urljoin(BASE_URL, href)
        parsed = urlparse(full_url)

        if parsed.netloc not in {
            "www.chromehearts.com",
            "chromehearts.com",
        }:
            return

        path = parsed.path.rstrip("/")
        if not path or path == "/":
            return

        # Ignore account, checkout, and informational pages.
        if re.search(
            r"/(account|cart|checkout|search|contact|about)(/|$)",
            path,
            re.I,
        ):
            return

        self.links.add(full_url.split("?")[0])


def fetch_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ProductMonitor/1.0)"
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def send_notification(title, message):
    request = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "shopping_bags",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError("Notification failed")


def main():
    html = fetch_page(BASE_URL)
    parser = ProductLinks()
    parser.feed(html)
    current = sorted(parser.links)

    if not current:
        raise RuntimeError(
            "No site links found. Check whether the website structure changed."
        )

    if STATE_FILE.exists():
        previous = set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    else:
        previous = set()

    if not previous:
        # First run establishes a baseline instead of alerting for every link.
        STATE_FILE.write_text(
            json.dumps(current, indent=2),
            encoding="utf-8",
        )
        print(f"Baseline saved: {len(current)} links")
        return

    new_links = sorted(set(current) - previous)

    for link in new_links:
        send_notification("Chrome Hearts: New page detected", link)

    STATE_FILE.write_text(
        json.dumps(sorted(previous | set(current)), indent=2),
        encoding="utf-8",
    )
    print(f"Checked {len(current)} links; new links: {len(new_links)}")


if __name__ == "__main__":
    main()
