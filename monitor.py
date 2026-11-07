
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

def notify(new_products, restocked_products=None):

    restocked_products = restocked_products or set()

    if not NTFY_TOPIC:
        raise RuntimeError(
            "NTFY_TOPIC is missing. Check GitHub Secrets."
        )

    topic = urllib.parse.quote(
        NTFY_TOPIC,
        safe="",
    )

    url = f"https://ntfy.sh/{topic}"

    sections = []
    if new_products:
        sections.append("신규 상품\n" + "\n".join(sorted(new_products)))
    if restocked_products:
        sections.append(
            "재입고 / 구매 가능 상태 감지 (사이즈 무관)\n"
            + "\n".join(sorted(restocked_products))
        )
    message = "\n\n".join(sections)

    request = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": "Chrome Hearts Alert",
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

    restocked_products = check_availability(current)

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

    if new_products or restocked_products:

        for product in sorted(new_products):
            print(
                f"NEW PRODUCT: {product}",
                flush=True,
            )

        notify(new_products, restocked_products)

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
