import json
import re
import sqlite3
import threading
import queue
import time
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

ASCII_ART = r"""
+++++-----------------------------------------------------------------------------------............
++++++-----------------------------------------------------------------------------------...........
+++++++-----------------------------------------------------------------------------------..........
++++++++-----------------------------------------------------------------------------------.........
+++++++++-----------------------------------------------------------------------------------........
++++++++++-----------------------------------------------------------------------------------.......
+++++++++++-----------------------------------------------------------------------------------......
++++++++++++---------------------------++##################+++---------------------------------.....
+++++++++++++---------------------+##############################+------------------------------....
++++++++++++++----------------+#####################################+----------------------------...
+++++++++++++++------------++##########################################+--------------------------..
++++++++++++++++---------+###############################################+-------------------------.
+++++++++++++++++------+####################################################------------------------
++++++++++++++++++----########################################################----------------------
+++++++++++++++++++++##########################################################+--------------------
+++++++++++++++++++#############################################################+-------------------
++++++++++++++++++################################################################------------------
+++++++++++++++++##################################################################-----------------
++++++++++++++++###########################+......................-#################----------------
+++++++++++++++##########################.........................-##################---------------
+++++++++++++++########################-..........................-##################+--------------
++++++++++++++########################-...........................-###################--------------
+++++++++++++#########################............................-###################+-------------
+++++++++++++########################-............................#####################-------------
+++++++++++++########################-...........................######################+------------
++++++++++++#########################-..............----------+########################+------------
++++++++++++#########################-.............####################################+------------
++++++++++++######################+--........................-##########################------------
++++++++++++#####################-...........................-##########################------------
++++++++++++####################.............................-#########################+------------
+++++++++++++##################+.............................-#########################+------------
+++++++++++++###################.............................-#########################-------------
+++++++++++++###################+...........................-#########################+-------------
++++++++++++++####################-.......................-+##########################--------------
++++++++++++++#######################+..........+####################################+--------------
+++++++++++++++#######################..........+####################################---------------
++++++++++++++++#######################-.........-##################################----------------
+++++++++++++++++########################++----+###################################-----------------
++++++++++++++++++################################################################------------------
+++++++++++++++++++##############################################################-------------------
++++++++++++++++++++###########################################################+--------------------
++++++++++++++++++++++########################################################----------------------
++++++++++++++++++++++++####################################################+-----------------------
++++++++++++++++++++++++++################################################+-------------------------
++++++++++++++++++++++++++++############################################+---------------------------
+++++++++++++++++++++++++++++++######################################+------------------------------
++++++++++++++++++++++++++++++++++###############################+----------------------------------
++++++++++++++++++++++++++++++++++++++++####################+++-------------------------------------
+++++++++++++++++++++++++++++++++++++++++++++++++++++++++-------------------------------------------
++++++++++++++++++++++++++++++++++++++++++++++++++++++++--------------------------------------------
+++++++++++++++++++++++++++++++++++++++++++++++++++++++---------------------------------------------
++++++++++++++++++++++++++++++++++++++++++++++++++++++----------------------------------------------
+++++++++++++++++++++++++++++++++++++++++++++++++++++-----------------------------------------------
++++++++++++++++++++++++++++++++++++++++++++++++++++------------------------------------------------
+++++++++++++++++++++++++++++++++++++++++++++++++++-------------------------------------------------
"""

ASCII_SIGNATURE = "Made By Isaac"

JSON_PATH = Path("accessories.json")
DB_PATH = Path("sources.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UGC-Community-Scraper/1.0)"
}

ID_PATTERNS = [
    re.compile(r"roblox\.com/catalog/(\d+)", re.IGNORECASE),
    re.compile(r"roblox\.com/library/(\d+)", re.IGNORECASE),
    re.compile(r"rbxassetid://(\d+)", re.IGNORECASE),
    re.compile(r'"assetId"\s*:\s*"?(\d+)"?', re.IGNORECASE),
    re.compile(r"\b(\d{6,15})\b"),
]

ROBLOX_PAGE_LIMIT = 30
ROBLOX_MAX_PAGES = 50
ROBLOX_PAGE_DELAY = 5
ROBLOX_MAX_RETRIES = 15

HTML_REQUEST_DELAY = 5
RATE_LIMIT_WAIT = 60
HTML_MAX_RETRIES = 15


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            discovered_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sources_asset_id
        ON sources(asset_id)
        """
    )
    conn.commit()
    return conn


def load_json_raw():
    if not JSON_PATH.exists():
        return {"accessories": []}
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"accessories": []}
    if not isinstance(data, dict):
        return {"accessories": []}
    accessories = data.get("accessories", [])
    if not isinstance(accessories, list):
        accessories = []
    cleaned = []
    for value in accessories:
        try:
            n = int(value)
            if n > 0:
                cleaned.append(n)
        except Exception:
            pass
    return {"accessories": cleaned}


def save_json_list(asset_ids):
    data = {"accessories": asset_ids}
    JSON_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def extract_ids(text):
    found = []
    for pattern in ID_PATTERNS:
        for match in pattern.findall(text):
            try:
                value = int(match)
                if value > 0:
                    found.append(value)
            except Exception:
                pass
    return found


def is_roblox_catalog_url(url):
    try:
        parsed = urlparse(url)
        return parsed.netloc.endswith("roblox.com") and parsed.path.startswith("/catalog")
    except Exception:
        return False


def build_roblox_catalog_api_url(page_url, cursor=None, limit=ROBLOX_PAGE_LIMIT):
    parsed = urlparse(page_url)
    params = parse_qs(parsed.query)

    api_params = {}

    simple_keys = [
        "CreatorName",
        "CreatorType",
        "salesTypeFilter",
        "SortType",
        "SortAggregation",
        "Category",
        "Subcategory",
        "IncludeNotForSale",
        "CreatorTargetId",
        "Keyword",
        "keyword",
        "category",
        "subcategory",
    ]

    for key in simple_keys:
        values = params.get(key)
        if values:
            api_params[key] = values[0]

    if "taxonomy" in params and params["taxonomy"]:
        api_params["taxonomy"] = params["taxonomy"][0]

    api_params["limit"] = str(limit)

    if cursor:
        api_params["cursor"] = cursor

    return "https://catalog.roblox.com/v1/search/items/details?" + urlencode(api_params)


def request_with_backoff(url, logger=None, max_retries=ROBLOX_MAX_RETRIES):
    attempt = 0

    while True:
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)

            if response.status_code == 429:
                if attempt >= max_retries:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except Exception:
                        wait_time = RATE_LIMIT_WAIT
                else:
                    wait_time = RATE_LIMIT_WAIT

                if logger:
                    logger(f"[RATE LIMIT] 429 received. Waiting {wait_time:.2f}s before retry...")
                time.sleep(wait_time)
                attempt += 1
                continue

            response.raise_for_status()
            return response

        except requests.RequestException as e:
            if attempt >= max_retries:
                raise e

            wait_time = RATE_LIMIT_WAIT
            if logger:
                logger(
                    f"[RETRY] Request failed ({e}). Waiting {wait_time:.2f}s...\n"
                    f"{ASCII_SIGNATURE}\n{ASCII_ART}"
                )
            time.sleep(wait_time)
            attempt += 1


def fetch_html_with_backoff(url, logger=None, max_retries=HTML_MAX_RETRIES):
    attempt = 0

    while True:
        if logger:
            logger(f"[HTML WAIT] Sleeping {HTML_REQUEST_DELAY}s before HTML request...")
        time.sleep(HTML_REQUEST_DELAY)

        try:
            response = requests.get(url, headers=HEADERS, timeout=25)

            if response.status_code == 429:
                if attempt >= max_retries:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = max(float(retry_after), RATE_LIMIT_WAIT)
                    except Exception:
                        wait_time = RATE_LIMIT_WAIT
                else:
                    wait_time = RATE_LIMIT_WAIT

                if logger:
                    logger(
                        f"[RATE LIMIT] 429 received. Waiting {wait_time:.2f}s before retry...\n"
                        f"{ASCII_SIGNATURE}\n{ASCII_ART}"
                    )
                time.sleep(wait_time)
                attempt += 1
                continue

            response.raise_for_status()
            return response.text

        except requests.RequestException as e:
            if attempt >= max_retries:
                raise e

            if logger:
                logger(
                    f"[HTML RATE LIMIT] 429 received from {url}. Waiting {wait_time:.2f}s before retry...\n"
                    f"{ASCII_SIGNATURE}\n{ASCII_ART}"
                )
            time.sleep(RATE_LIMIT_WAIT)
            attempt += 1


def scrape_roblox_catalog_url(url, logger=None, max_pages=ROBLOX_MAX_PAGES, limit=ROBLOX_PAGE_LIMIT):
    cursor = None
    page_count = 0

    while page_count < max_pages:
        api_url = build_roblox_catalog_api_url(url, cursor=cursor, limit=limit)

        if logger:
            logger(f"[ROBLOX API] Fetching page {page_count + 1}: {api_url}")

        response = request_with_backoff(api_url, logger=logger)
        payload = response.json()
        items = payload.get("data", []) or []

        page_ids = []
        for item in items:
            item_id = item.get("id")
            if item_id:
                try:
                    page_ids.append(int(item_id))
                except Exception:
                    pass

        if logger:
            logger(f"[ROBLOX API] Page {page_count + 1} returned {len(page_ids)} items")

        yield page_ids

        cursor = payload.get("nextPageCursor")
        page_count += 1

        if not cursor:
            break

        time.sleep(ROBLOX_PAGE_DELAY)


def scrape_url(url, logger=None):
    if is_roblox_catalog_url(url):
        if logger:
            logger(f"[FETCH ROBLOX CATALOG API] {url}")
        return scrape_roblox_catalog_url(url, logger=logger)

    if logger:
        logger(f"[FETCH HTML] {url}")

    html = fetch_html_with_backoff(url, logger=logger)
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    candidates.extend(extract_ids(html))
    candidates.extend(extract_ids(soup.get_text(" ", strip=True)))

    if logger:
        logger(f"[FOUND] {len(candidates)} candidate IDs from HTML")

    return [candidates]


def get_db_asset_ids(conn):
    cur = conn.cursor()
    cur.execute("SELECT asset_id FROM sources")
    rows = cur.fetchall()
    found = set()
    for row in rows:
        try:
            found.add(int(row[0]))
        except Exception:
            pass
    return found


def store_sources(conn, asset_ids, source_url, allow_duplicates=False):
    cur = conn.cursor()
    timestamp = datetime.now(timezone.utc).isoformat()
    existing_pairs = set()

    if not allow_duplicates:
        cur.execute("SELECT asset_id, source_url FROM sources")
        existing_pairs = {(str(a), s) for a, s in cur.fetchall()}

    inserts = 0
    for asset_id in asset_ids:
        pair = (str(asset_id), source_url)
        if not allow_duplicates and pair in existing_pairs:
            continue

        cur.execute(
            "INSERT INTO sources (asset_id, source_url, discovered_at) VALUES (?, ?, ?)",
            (str(asset_id), source_url, timestamp),
        )
        inserts += 1

    conn.commit()
    return inserts


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("UGC Community Scraper")
        self.root.geometry("900x700")
        self.log_queue = queue.Queue()
        self.running = False

        self.allow_duplicates_var = tk.BooleanVar(value=False)
        self.check_json_var = tk.BooleanVar(value=True)
        self.check_db_var = tk.BooleanVar(value=True)

        self._build_ui()
        self.root.after(100, self._drain_log_queue)

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Community URLs (one per line)").pack(anchor="w")
        self.url_text = tk.Text(main, height=12, wrap="word")
        self.url_text.pack(fill="x", pady=(4, 12))

        options = ttk.LabelFrame(main, text="Options", padding=10)
        options.pack(fill="x", pady=(0, 12))

        ttk.Checkbutton(
            options,
            text="Allow duplicates",
            variable=self.allow_duplicates_var,
            command=self._sync_duplicate_options,
        ).grid(row=0, column=0, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options,
            text="Check JSON for duplicates",
            variable=self.check_json_var,
        ).grid(row=0, column=1, sticky="w", padx=(0, 20))

        ttk.Checkbutton(
            options,
            text="Check DB for duplicates",
            variable=self.check_db_var,
        ).grid(row=0, column=2, sticky="w")

        files = ttk.Frame(main)
        files.pack(fill="x", pady=(0, 12))
        ttk.Label(files, text=f"JSON: {JSON_PATH.resolve()}").pack(anchor="w")
        ttk.Label(files, text=f"DB: {DB_PATH.resolve()}").pack(anchor="w")

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(0, 12))

        self.run_btn = ttk.Button(buttons, text="Scrape and Update", command=self.start_scrape)
        self.run_btn.pack(side="left")

        ttk.Button(buttons, text="Load sample", command=self.load_sample).pack(side="left", padx=8)
        ttk.Button(buttons, text="Clear URLs", command=lambda: self.url_text.delete("1.0", "end")).pack(side="left")

        ttk.Label(main, text="Log").pack(anchor="w")
        self.log_text = tk.Text(main, height=18, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _sync_duplicate_options(self):
        allow = self.allow_duplicates_var.get()
        if allow:
            self.check_json_var.set(False)
            self.check_db_var.set(False)

    def load_sample(self):
        sample = (
            "https://www.roblox.com/catalog?taxonomy=tZsUsd2BqGViQrJ9Vs3Wah&CreatorName=On+Clearance&CreatorType=Group&salesTypeFilter=1\n"
            "https://example.com/community-page-2\n"
        )
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", sample)

    def log(self, message):
        self.log_queue.put(message)

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def start_scrape(self):
        if self.running:
            return

        urls = [u.strip() for u in self.url_text.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning("No URLs", "Please enter at least one URL.")
            return

        self.running = True
        self.run_btn.configure(state="disabled")
        threading.Thread(target=self._scrape_worker, args=(urls,), daemon=True).start()

    def _scrape_worker(self, urls):
        conn = None
        try:
            conn = init_db()
            existing_json_list = load_json_raw()["accessories"]
            existing_json_set = set(existing_json_list)
            existing_db_set = get_db_asset_ids(conn)

            allow_duplicates = self.allow_duplicates_var.get()
            check_json = self.check_json_var.get()
            check_db = self.check_db_var.get()

            output_json = list(existing_json_list)
            added_ids = []
            skipped_json = 0
            skipped_db = 0
            total_scraped_candidates = 0
            total_db_inserts = 0

            self.log(f"[START] URLs entered: {len(urls)}")
            self.log(f"[START] Allow duplicates: {allow_duplicates}")
            self.log(f"[START] Check JSON duplicates: {check_json}")
            self.log(f"[START] Check DB duplicates: {check_db}")

            for url in urls:
                try:
                    page_stream = scrape_url(url, logger=self.log)

                    for page_index, ids in enumerate(page_stream, start=1):
                        total_scraped_candidates += len(ids)
                        self.log(f"[PAGE] {url} -> page {page_index}, {len(ids)} candidate IDs")

                        accepted_for_source = []
                        for asset_id in ids:
                            if not allow_duplicates:
                                if check_json and asset_id in existing_json_set:
                                    skipped_json += 1
                                    continue
                                if check_db and asset_id in existing_db_set:
                                    skipped_db += 1
                                    continue

                            output_json.append(asset_id)
                            added_ids.append(asset_id)
                            accepted_for_source.append(asset_id)
                            existing_json_set.add(asset_id)
                            existing_db_set.add(asset_id)

                        inserted = store_sources(
                            conn,
                            accepted_for_source if not allow_duplicates else ids,
                            url,
                            allow_duplicates=allow_duplicates,
                        )
                        total_db_inserts += inserted

                        if not allow_duplicates:
                            save_json_list(sorted(set(output_json)))
                        else:
                            save_json_list(output_json)

                        self.log(f"[STORE] Page {page_index}: {inserted} DB rows written")
                        self.log(f"[SAVE] Progress saved after page {page_index}")

                except Exception as e:
                    self.log(f"[ERROR] {url} -> {e}")

            self.log("")
            self.log(f"[DONE] Raw scraped candidates: {total_scraped_candidates}")
            self.log(f"[DONE] Added to JSON: {len(added_ids)}")
            self.log(f"[DONE] Skipped by JSON duplicate check: {skipped_json}")
            self.log(f"[DONE] Skipped by DB duplicate check: {skipped_db}")
            self.log(f"[DONE] DB inserts: {total_db_inserts}")
            self.log(f"[DONE] JSON saved to: {JSON_PATH.resolve()}")
            self.log(f"[DONE] DB saved to: {DB_PATH.resolve()}")

        finally:
            if conn is not None:
                conn.close()
            self.running = False
            self.root.after(0, lambda: self.run_btn.configure(state="normal"))


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
