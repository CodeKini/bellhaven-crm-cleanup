import csv
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://analyst-assessment-production.up.railway.app/communities"
OUTPUT_FILE = "bellhaven_locations.csv"

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; BeginnerBellhavenScraper/1.0)"})


def get_soup(url):
    response = session.get(url, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def scrape_community(url):
    soup = get_soup(url)
    main = soup.find("main") or soup.body
    name_tag = main.find("h1")
    if name_tag is None:
        raise ValueError(f"Community name not found: {url}")

    # The detail pages display labels followed by their values.
    parts = list(main.stripped_strings)
    address_start = parts.index("Address") + 1
    offerings_start = parts.index("Care Offerings")
    address_parts = parts[address_start:offerings_start]

    # The final address line looks like: Maplewood, OH 44280.
    city_line = next(
        (part for part in reversed(address_parts)
         if re.fullmatch(r".+?,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?", part)),
        None,
    )
    if city_line is None:
        raise ValueError(f"City, state, or ZIP not found: {url}")

    match = re.fullmatch(r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", city_line)
    street = " ".join(address_parts[:address_parts.index(city_line)]).strip()
    if not street:
        raise ValueError(f"Street address not found: {url}")

    offerings_end = next(
        (i for i in range(offerings_start + 1, len(parts))
         if parts[i] in {"Administrator", "Phone"}),
        len(parts),
    )
    offerings = "; ".join(parts[offerings_start + 1:offerings_end])

    return {
        "name": name_tag.get_text(" ", strip=True),
        "street_address": street,
        "city": match.group(1).strip(),
        "state": match.group(2),
        "zip_code": match.group(3),
        "care_offerings": offerings,
    }


def main():
    community_urls = []
    seen = set()
    page_url = BASE_URL

    # Follow Next links, so we collect all three listing pages.
    while page_url:
        print(f"Reading listing: {page_url}")
        soup = get_soup(page_url)
        main_content = soup.find("main") or soup.body

        for heading in main_content.find_all("h3"):
            link = heading.find("a", href=True)
            if not link:
                continue
            url = urljoin(page_url, link["href"])
            if url not in seen and "/communities/" in url:
                seen.add(url)
                community_urls.append(url)

        next_link = main_content.find("a", string=re.compile(r"Next", re.I))
        page_url = urljoin(page_url, next_link["href"]) if next_link else None

    print(f"Found {len(community_urls)} community links.")
    rows = []
    for index, url in enumerate(community_urls, start=1):
        print(f"Scraping {index}/{len(community_urls)}: {url}")
        rows.append(scrape_community(url))
        time.sleep(0.2)  # A short pause between requests is polite.

    columns = ["name", "street_address", "city", "state", "zip_code", "care_offerings"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done! Saved {len(rows)} communities to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
