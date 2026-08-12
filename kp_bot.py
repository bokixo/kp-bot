"""
KP -> Discord bot (verzija za GitHub Actions)
----------------------------------------------
Prati dve pretrage na KupujemProdajem (SSD i RAM), filtrira po ceni i lokaciji
(Beograd), i za svaki NOVI oglas salje poruku na odgovarajuci Discord kanal
preko webhook-a.

Ova verzija je napravljena da se pokrene JEDNOM i ugasi (ne radi u petlji) -
GitHub Actions je taj koji je pokrece iznova na svakih par minuta po rasporedu
(vidi .github/workflows/kp_bot.yml).

Webhook linkovi se NE nalaze u ovom fajlu (jer je repozitorijum javan) - citaju
se iz "environment varijabli" SSD_WEBHOOK_URL i RAM_WEBHOOK_URL, koje se u
GitHub Actions-u postavljaju kroz repository Secrets.
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
from datetime import datetime

# ============ PODESAVANJA - OVDE MOZES DA MENJAS ============

SEARCHES = [
    {
        "name": "SSD",
        "keywords": "nvme",
        "category_path": "kompjuteri-desktop/hard-diskovi-ssd",
        "category_id": 10,
        "group_id": 1350,
        "max_price_din": 5000,
        "webhook_env": "SSD_WEBHOOK_URL",
    },
    {
        "name": "RAM (DDR4)",
        "keywords": "ddr4",
        "category_path": "kompjuteri-desktop/ram-memorije",
        "category_id": 10,
        "group_id": 93,
        "max_price_din": 3000,
        "webhook_env": "RAM_WEBHOOK_URL",
    },
]

LOCATION_FILTER = "beograd"    # trazi se da li ova rec postoji u lokaciji oglasa
EUR_TO_RSD = 117.5             # okvirni kurs, koristi se samo ako je cena data u evrima
SEEN_FILE = "vidjeni_oglasi.json"   # ovde se pamte oglasi koje je bot vec video

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ============ KRAJ PODESAVANJA ============


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen_ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_ids), f)


def parse_price_to_din(text):
    """Pokusava da izvuce cenu iz teksta oglasa i vrati je u dinarima.
    Vraca None ako cena nije prepoznata (npr. 'Kontakt')."""
    text = text.replace("\xa0", " ")

    din_match = re.search(r'([\d\.]+)\s*din', text)
    if din_match:
        num = din_match.group(1).replace(".", "")
        try:
            return int(num)
        except ValueError:
            return None

    eur_match = re.search(r'([\d\.,]+)\s*€', text)
    if eur_match:
        num = eur_match.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num) * EUR_TO_RSD
        except ValueError:
            return None

    return None


def fetch_ads(search):
    """Ucitava rezultate pretrage sa KupujemProdajem, sortirano po najnovijim.
    Ako je u podesavanjima data kategorija (category_path/category_id/group_id),
    pretraga se ogranicava samo na tu kategoriju."""
    if search.get("category_path"):
        url = f"https://www.kupujemprodajem.com/{search['category_path']}/pretraga"
    else:
        url = "https://www.kupujemprodajem.com/pretraga"

    params = {"keywords": search.get("keywords", ""), "so": "1"}
    if search.get("category_id"):
        params["categoryId"] = search["category_id"]
    if search.get("group_id"):
        params["groupId"] = search["group_id"]

    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    ads = []
    seen_hrefs = set()

    for a in soup.find_all("a", href=True):
        m = re.search(r'/oglas/(\d+)', a["href"])
        if not m:
            continue
        ad_id = m.group(1)
        title = a.get_text(strip=True)
        if not title:
            continue  # ovo je verovatno link oko slike, preskacemo
        if ad_id in seen_hrefs:
            continue
        seen_hrefs.add(ad_id)

        # trazimo roditeljski deo stranice (container) koji sadrzi i cenu i lokaciju
        container = a
        container_text = ""
        for _ in range(6):
            if container.parent is None:
                break
            container = container.parent
            container_text = container.get_text(" | ", strip=True)
            if 40 < len(container_text) < 1200:
                break

        price_din = parse_price_to_din(container_text)
        location_ok = LOCATION_FILTER in container_text.lower()

        link = a["href"]
        if link.startswith("/"):
            link = "https://www.kupujemprodajem.com" + link

        ads.append({
            "id": ad_id,
            "title": title,
            "link": link,
            "price_din": price_din,
            "location_ok": location_ok,
        })

    return ads


def send_to_discord(webhook_url, ad, search_name):
    if ad["price_din"]:
        price_str = f"{int(ad['price_din']):,} din".replace(",", ".")
    else:
        price_str = "cena nije prepoznata"

    payload = {
        "embeds": [{
            "title": ad["title"][:250],
            "url": ad["link"],
            "description": f"💰 {price_str}",
            "color": 3066993,
            "footer": {"text": f"KP pretraga: {search_name}"},
        }]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
        if r.status_code >= 300:
            print(f"[GRESKA] Discord webhook vratio status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[GRESKA] Slanje na Discord nije uspelo: {e}")


def check_search(search, seen_ids):
    webhook_url = os.environ.get(search["webhook_env"])
    if not webhook_url:
        print(f"[GRESKA] Nedostaje environment varijabla {search['webhook_env']} "
              f"- da li si dodao Secret u GitHub repozitorijum?")
        return

    try:
        ads = fetch_ads(search)
    except Exception as e:
        print(f"[GRESKA] Ne mogu da ucitam pretragu '{search['name']}': {e}")
        return

    new_count = 0
    for ad in ads:
        if ad["id"] in seen_ids:
            continue
        seen_ids.add(ad["id"])

        if not ad["location_ok"]:
            continue
        if ad["price_din"] is None or ad["price_din"] > search["max_price_din"]:
            continue

        send_to_discord(webhook_url, ad, search["name"])
        new_count += 1
        time.sleep(1)  # da ne saljemo prebrzo ka Discordu

    print(f"[{datetime.now().strftime('%H:%M:%S')}] {search['name']}: "
          f"provereno {len(ads)} oglasa, poslato {new_count} novih")


def main():
    seen_ids = load_seen()
    first_run = not seen_ids

    if first_run:
        print("Prvo pokretanje - belezim postojece oglase bez slanja notifikacija...")
        for search in SEARCHES:
            try:
                ads = fetch_ads(search)
                for ad in ads:
                    seen_ids.add(ad["id"])
            except Exception as e:
                print(f"[GRESKA] {e}")
        save_seen(seen_ids)
        print("Gotovo. Od sledeceg pokretanja se salju samo NOVI oglasi.")
        return

    for search in SEARCHES:
        check_search(search, seen_ids)
    save_seen(seen_ids)


if __name__ == "__main__":
    main()
