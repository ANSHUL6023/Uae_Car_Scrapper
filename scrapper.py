#!/usr/bin/env python3
"""
scrapper.py — ZigWheels UAE (zigwheels.ae) car scraper

Scrapes a single car model page from https://www.zigwheels.ae/new-cars/{brand}/{model}
(plus its /specifications, /colors and /faq sub-pages) and produces a structured
dict, ready to be handed to acf_mapper.py for a WordPress ACF import.

This replaces the previous India-focused Next.js/RSC scraper. zigwheels.ae is
a different, server-rendered platform with no Next.js payloads or variant API, so
the parsing layer below is a full rewrite. The generic pieces — Gemini content
rephrasing, Vertex AI logo removal, Selenium fallback, retrying fetch() — are kept
as-is (only the source-site Referer/base URL changed).

Usage
-----
  python scrapper.py --url https://www.zigwheels.ae/new-cars/hyundai/creta \\
      --output hyundai-creta --outdir ./pipeline_output --remove-logo --rephrase-content

  python scrapper.py --sitemap https://www.zigwheels.ae/sitemap.xml --max 20
"""

import argparse
import base64
import csv
import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cv2
import numpy as np
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_driver = None
USE_SELENIUM = False

# Set from --remove-logo in main(); read by _download_and_convert_images()
REMOVE_LOGO = False

# Set from --max-images in main(); caps how many gallery photos get scraped per model
MAX_IMAGES = 5

# Set from --logo-delay in main(); seconds to wait between Vertex AI logo-removal
# calls so images don't get fired at the API back-to-back.
LOGO_REMOVAL_DELAY = 15.0

ZIGWHEELS_BASE = "https://www.zigwheels.ae"

# ============================================================================
# Content rephrasing config (Gemini via GEMINI_API_KEY in .env)
# ============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_REPHRASE_MODEL = os.getenv("GEMINI_REPHRASE_MODEL", "gemini-2.5-flash")

# ============================================================================
# Logo removal config (Vertex AI, authenticated via local OAuth files —
# NOT via .env). Point these at the files sitting in your project directory.
# ============================================================================
LOGO_TOKEN_FILE = "token.json"                 # saved/refreshed OAuth token
LOGO_CLIENT_SECRET_FILE = "client_secret.json" # downloaded from Google Cloud Console
LOGO_MASK_FILE = "mask.png"                    # marks where the source logo sits
LOGO_MASK2_FILE = "mask2.png"
VERTEX_PROJECT_ID = "content-engine-bot"
VERTEX_LOCATION = "us-central1"
VERTEX_IMAGE_MODEL = "gemini-2.5-flash-image"
LOGO_REMOVE_PROMPT = (
"Remove the logo inside the masked area. Blend the empty space seamlessly with the surrounding sky and background."
)


# ============================================================================
# Content rephrasing (Gemini) — rewrites post_content + whats-new lists so
# republished content reads differently from the source article.
# ============================================================================
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to a .env file next to "
                "scrapper.py (see .env.example)."
            )
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def _rephrase_response_schema():
    from google.genai import types
    item_schema = lambda field: types.Schema(
        type=types.Type.ARRAY,
        items=types.Schema(type=types.Type.OBJECT, properties={field: types.Schema(type=types.Type.STRING)}),
    )
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "post_content": types.Schema(type=types.Type.STRING),
            "introduction_list": item_schema("introduction_item"),
            "exterior_list": item_schema("exterior_item"),
            "interior_list": item_schema("interior_item"),
            "powertrain_list": item_schema("powertrain_item"),
        },
    )


def rephrase_model_dict(data: dict, retries: int = 2, retry_delay: float = 2.0) -> dict:
    """Rephrase post_content + acf_whats_new lists of one ACF-mapped model dict, in place."""
    from google.genai import types

    whats_new = data.get("acf_whats_new", {}) or {}
    content_to_rephrase = {
        "post_content": data.get("post_content", ""),
        "introduction_list": whats_new.get("introduction_list", []),
        "exterior_list": whats_new.get("exterior_list", []),
        "interior_list": whats_new.get("interior_list", []),
        "powertrain_list": whats_new.get("powertrain_list", []),
    }
    has_content = content_to_rephrase["post_content"] or any(
        content_to_rephrase[k]
        for k in ("introduction_list", "exterior_list", "interior_list", "powertrain_list")
    )
    if not has_content:
        return data

    client = _get_gemini_client()
    prompt = (
        "Rephrase this content cleanly in your own words. Preserve every fact, "
        "spec number and meaning exactly — only change the wording/sentence "
        "structure. Keep lists formatted as requested:\n\n"
        + json.dumps(content_to_rephrase)
    )
    label = data.get("post_title") or data.get("model") or data.get("url", "?")

    for attempt in range(1, retries + 2):
        try:
            response = client.models.generate_content(
                model=GEMINI_REPHRASE_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_rephrase_response_schema(),
                    temperature=0.4,
                ),
            )
            rephrased = json.loads(response.text)
            data["post_content"] = rephrased.get("post_content", data.get("post_content", ""))
            data.setdefault("acf_whats_new", {})
            for key in ("introduction_list", "exterior_list", "interior_list", "powertrain_list"):
                if key in rephrased:
                    data["acf_whats_new"][key] = rephrased[key]
            log.info("  [rephrase] done: %s", label)
            return data
        except Exception as exc:
            if attempt <= retries:
                time.sleep(retry_delay)
                continue
            log.warning("  [rephrase] failed for %s: %s", label, exc)
            return data
    return data


def rephrase_models(models: list, delay_between: float = 1.0) -> list:
    """Rephrase a whole list of ACF-mapped model dicts, in place."""
    total = len(models)
    for i, m in enumerate(models, 1):
        log.info("  [rephrase] %d/%d", i, total)
        rephrase_model_dict(m)
        if i < total:
            time.sleep(delay_between)
    return models


# ============================================================================
# Logo removal (Vertex AI image editing) — strips the source site's logo /
# watermark from each downloaded gallery photo before it's saved locally.
# Auth uses a local OAuth token.json + client_secret.json (installed-app
# flow), NOT .env / service-account credentials.
# ============================================================================
_logo_creds = None
_logo_mask_bytes = None
_LOGO_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _load_logo_client_info():
    """Read client_id/client_secret/token_uri out of client_secret.json (either
    the 'installed' or 'web' style Google Cloud Console download)."""
    if not os.path.exists(LOGO_CLIENT_SECRET_FILE):
        return {}
    with open(LOGO_CLIENT_SECRET_FILE) as f:
        cs = json.load(f)
    section = cs.get("installed") or cs.get("web") or {}
    return {
        "client_id": section.get("client_id"),
        "client_secret": section.get("client_secret"),
        "token_uri": section.get("token_uri", "https://oauth2.googleapis.com/token"),
    }


import google.auth


def get_vertex_auth_client():
    global _logo_creds
    if _logo_creds and _logo_creds.valid:
        return _logo_creds

    if os.path.exists(LOGO_TOKEN_FILE):
        info = _load_logo_client_info()
        creds = Credentials.from_authorized_user_file(LOGO_TOKEN_FILE, _LOGO_SCOPES)
        if info.get("client_id"):
            creds._client_id = info["client_id"]
            creds._client_secret = info["client_secret"]
            creds._token_uri = info["token_uri"]
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(LOGO_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        _logo_creds = creds
        return _logo_creds

    # Fall back to Application Default Credentials
    creds, _ = google.auth.default(scopes=_LOGO_SCOPES)
    creds.refresh(Request())
    _logo_creds = creds
    return _logo_creds


def _get_logo_mask_bytes(_logo_mask_file: str) -> bytes:
    global _logo_mask_bytes
    if _logo_mask_bytes is None:
        with open(_logo_mask_file, "rb") as f:
            _logo_mask_bytes = f.read()
    return _logo_mask_bytes


def remove_logo_from_bytes(image_bytes: bytes, retries: int = 2, timeout: int = 60) -> bytes:
    """Send one image + mask to Vertex AI (Gemini image model) and return the
    edited image bytes with the source-site logo removed."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    template1 = cv2.imread(LOGO_MASK_FILE)
    template2 = cv2.imread(LOGO_MASK2_FILE)
    mask_image = LOGO_MASK_FILE
    res1 = cv2.matchTemplate(img, template1, cv2.TM_CCOEFF_NORMED)
    _, max_val1, _, max_loc1 = cv2.minMaxLoc(res1)

    res2 = cv2.matchTemplate(img, template2, cv2.TM_CCOEFF_NORMED)
    _, max_val2, _, max_loc2 = cv2.minMaxLoc(res2)
    THRESHOLD = 0.5

    if max_val1 > THRESHOLD and max_val1 > max_val2:
        mask_image = LOGO_MASK_FILE
    elif max_val2 > THRESHOLD:
        mask_image = LOGO_MASK2_FILE

    mask_bytes = _get_logo_mask_bytes(mask_image)

    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{VERTEX_PROJECT_ID}"
        f"/locations/{VERTEX_LOCATION}/publishers/google/models/{VERTEX_IMAGE_MODEL}:generateContent"
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inlineData": {"data": base64.b64encode(image_bytes).decode(), "mimeType": "image/png"}},
                {"inlineData": {"data": base64.b64encode(mask_bytes).decode(), "mimeType": "image/png"}},
                {"text": LOGO_REMOVE_PROMPT},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "topP": 0.95,
            "imageConfig": {"aspectRatio": "16:9"},
        },
    }

    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            creds = get_vertex_auth_client()
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if not resp.ok:
                body = resp.text[:800]
                raise RuntimeError(f"{resp.status_code} {resp.reason} — response body: {body}")
            ai_response = resp.json()
            out_b64 = ai_response["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            return base64.b64decode(out_b64)
        except Exception as exc:
            last_exc = exc
            log.debug("    Logo removal attempt %d/%d failed: %s", attempt, retries + 1, exc)
            time.sleep(1.5)
    raise RuntimeError(f"Logo removal failed after {retries + 1} attempts: {last_exc}")


def _init_selenium():
    global _driver
    if _driver:
        return _driver
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        service = Service(ChromeDriverManager().install())
        _driver = webdriver.Chrome(service=service, options=opts)
        _driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        log.info("Selenium Chrome driver initialised")
        return _driver
    except Exception as exc:
        log.error("Selenium init failed: %s", exc)
        log.error("Install: pip install selenium webdriver-manager")
        sys.exit(1)


def _selenium_fetch(url: str, wait: float = 3.0) -> tuple[BeautifulSoup | None, str]:
    driver = _init_selenium()
    try:
        driver.get(url)
        time.sleep(wait)
        html = driver.page_source
        return BeautifulSoup(html, "lxml"), html
    except Exception as exc:
        log.warning("Selenium fetch error: %s", exc)
        return None, ""


BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": f"{ZIGWHEELS_BASE}/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)


def fetch(url: str, retries: int = 3, delay: float = 2.0) -> tuple[BeautifulSoup | None, str]:
    """GET a URL and return (BeautifulSoup, raw_html), or (None, '') on failure."""
    if USE_SELENIUM:
        return _selenium_fetch(url)

    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, timeout=30)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "lxml"), resp.text
            if resp.status_code == 403:
                log.warning("403 Forbidden – site may require Selenium. Try --selenium flag.")
            else:
                log.warning("HTTP %s  (%s)  attempt %d", resp.status_code, url, attempt)
        except requests.RequestException as exc:
            log.warning("Request error: %s  attempt %d", exc, attempt)
        if attempt < retries:
            time.sleep(delay * attempt + random.uniform(0, 1))
    return None, ""


def parse_sitemap(sitemap_url: str) -> list:
    """Recursively parse a zigwheels.ae sitemap index or sitemap XML and
    return a list of car-model page URLs (/new-cars/{brand}/{model})."""
    log.info("Parsing sitemap: %s", sitemap_url)
    soup, _ = fetch(sitemap_url)
    if soup is None:
        log.error("Could not fetch sitemap: %s", sitemap_url)
        return []

    child_sitemaps = soup.find_all("sitemap")
    if child_sitemaps:
        urls = []
        for sm in child_sitemaps:
            loc = sm.find("loc")
            if loc:
                urls.extend(parse_sitemap(loc.text.strip()))
                time.sleep(0.5)
        return urls

    locs = [loc.text.strip() for loc in soup.find_all("loc")]

    # Keep only model overview pages: /new-cars/{brand}/{model}
    # (exclude brand index pages, body-type/fuel-type listing pages, and every
    # car sub-page: /specifications, /colors, /price, /promo, /images, /news,
    # /user-reviews, /faq, /dealers/*, compare-cars, car-news, car-tips, etc.)
    car_urls = []
    for u in locs:
        m = re.search(r"/new-cars/([a-z0-9-]+)/([a-z0-9-]+)$", u, re.I)
        if not m:
            continue
        brand_seg, model_seg = m.group(1), m.group(2)
        if brand_seg in {"best-suv", "best-sedan", "best-hatchback", "electric", "petrol",
                          "diesel", "hybrid", "phev", "automatic"}:
            continue
        car_urls.append(u)
    log.info("Found %d car-model URLs in sitemap", len(car_urls))
    return car_urls


# ============================================================================
# zigwheels.ae page parsing
#
# zigwheels.ae is a plain server-rendered site (CSRF-token meta tags, no
# Next.js/__NEXT_DATA__, no variant JSON API) — a different shape entirely from
# Next.js RSC setup. Everything below is parsed straight out of the rendered
# HTML: real <table> elements for key specs / variant pricing / similar-cars
# comparisons, heading-delimited sections for categorized specs and FAQs, and
# the imagecdn.zigwheels.ae URL pattern for gallery photos.
#
# If zigwheels.ae changes its markup, the most likely breakage points are the
# table/heading lookups below (_find_table_after / _find_heading) — check
# those first with a saved copy of a live page.
# ============================================================================

IMG_CDN_RE = re.compile(
    r"https://imagecdn\.zigwheels\.ae/[a-z]+/gallery/(exterior|interior|color)/(\d+)/(\d+)/([^\"'()\s]+?\.(?:jpe?g|png|webp))",
    re.I,
)


def _brand_model_from_url(url: str) -> tuple[str, str]:
    """https://www.zigwheels.ae/new-cars/hyundai/creta -> ('hyundai', 'creta')"""
    m = re.search(r"/new-cars/([a-z0-9-]+)/([a-z0-9-]+)", url, re.I)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _find_heading(soup: BeautifulSoup, pattern: str):
    """First h1-h4 whose text matches pattern (case-insensitive, partial)."""
    rx = re.compile(pattern, re.I)
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        if rx.search(tag.get_text(" ", strip=True)):
            return tag
    return None


def _find_table_after(soup: BeautifulSoup, heading_pattern: str, max_hops: int = 40):
    """Return the first <table> that appears after a heading matching
    heading_pattern, before the next heading of the same or higher level."""
    heading = _find_heading(soup, heading_pattern)
    if heading is None:
        return None
    node = heading
    hops = 0
    while node is not None and hops < max_hops:
        node = node.find_next(["table", "h1", "h2", "h3", "h4"])
        hops += 1
        if node is None:
            return None
        if node.name == "table":
            return node
        if node.name in ("h1", "h2", "h3", "h4"):
            return None
    return None


def _table_rows(table) -> list[list[str]]:
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = [_clean_ws(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        # Keep the row's link too (variant name cells are links)
        link = tr.find("a", href=True)
        href = urljoin(ZIGWHEELS_BASE, link["href"]) if link else ""
        if any(cells):
            rows.append(cells + [href])
    return rows


KEY_SPEC_LABELS = [
    "Transmission", "Drive Type", "Seating", "Engine", "Battery Capacity",
    "Range", "Top Speed", "Power", "Torque", "Mileage", "Fuel Type", "Airbag",
]


def _extract_key_specs(soup: BeautifulSoup) -> dict:
    """The 'Key Specs of <Model> <Year>' box near the top of the overview page."""
    specs: dict[str, str] = {}
    table = _find_table_after(soup, r"Key Specs of")
    if table:
        for row in _table_rows(table):
            if len(row) >= 2 and row[0]:
                specs[row[0]] = row[1]
    if specs:
        return specs

    # Fallback: scan visible text near the heading using the known label set.
    heading = _find_heading(soup, r"Key Specs of")
    if heading is None:
        return specs
    chunk = heading.find_next(string=True)
    text = ""
    node = heading
    for _ in range(80):
        node = node.find_next(string=True)
        if node is None:
            break
        text += " " + str(node)
        if len(text) > 1500:
            break
    for i, label in enumerate(KEY_SPEC_LABELS):
        nxt_labels = "|".join(re.escape(l) for l in KEY_SPEC_LABELS if l != label)
        m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([^|]+?)(?=(?:{nxt_labels})|$)", text, re.I)
        if m:
            val = _clean_ws(m.group(1))[:80]
            if val:
                specs[label] = val
    return specs


def _extract_price(soup: BeautifulSoup, html: str) -> dict:
    """Price range, currency, city and monthly-installment figure."""
    text = soup.get_text(" ", strip=True)
    out = {
        "price_min_aed": "", "price_max_aed": "", "price_display": "",
        "price_city": "", "monthly_installment_aed": "", "price_status": "",
    }
    # NOTE: this used to be "AED?" (regex "optional D", i.e. matches "AE"/"AED"
    # literally) instead of "(?:AED)?" (optional whole word "AED"). The live
    # page's actual text is "AED 70,000 - 82,000 Price in Abu Dhabi" — no
    # second "AED" before the high price — so the old pattern could never
    # match a real range at all and price_min/max/display came back empty
    # even though the number was right there in the page text.
    m = re.search(
        r"AED\s*([\d,]+)(?:\s*-\s*(?:AED)?\s*([\d,]+))?\s*Price in\s*"
        r"([A-Za-z][A-Za-z ]*?)(?=\s*(?:Monthly|VIEW|$|\.))", text)
    if m:
        lo = m.group(1).replace(",", "")
        hi = (m.group(2) or "").replace(",", "")
        out["price_min_aed"] = lo
        out["price_max_aed"] = hi or lo
        out["price_display"] = f"AED {m.group(1)}" + (f" - {m.group(2)}" if m.group(2) else "")
        out["price_city"] = _clean_ws(m.group(3))
    elif re.search(r"Price coming soon", text, re.I):
        out["price_status"] = "coming_soon"

    m2 = re.search(r"Monthly from AED\s*([\d,]+)", text)
    if m2:
        out["monthly_installment_aed"] = m2.group(1).replace(",", "")

    return out


def _extract_rating(soup: BeautifulSoup) -> tuple[str, str]:
    text = soup.get_text(" ", strip=True)
    m = re.search(r"\b(\d(?:\.\d)?)\s*\|\s*(\d+)\s*Reviews\b", text)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _extract_variants(soup: BeautifulSoup) -> list[dict]:
    """Variant pricing table (e.g. under the 'Petrol' / 'Hybrid' sub-heading,
    before '5 Things to Know'). Each row: name+url | price | spec string |
    badge | 'View Offers' | 'Compare'."""
    variants = []
    variants_heading = soup.find(id="sec-variants") or _find_heading(soup, r"^Variant$")

    # Walk EVERY table that appears after the anchor/heading, in document
    # order, and take the first one that actually has a priced row. A
    # single-hop "next table or bail" check used to live here, but the real
    # variant-pricing table normally sits under its own "<Model> Price In
    # UAE" heading further down the page — one hop lands on that heading
    # (or on the earlier "Key Specs" table) and gives up before ever
    # reaching it, so variants silently came back empty.
    candidates = variants_heading.find_all_next("table") if variants_heading is not None else soup.find_all("table")

    table = None
    for t in candidates:
        rows = _table_rows(t)
        # Check if table has priced rows, OR rows that look like a variant (cc, hp, Compare, etc.)
        # This handles cars where ALL variants are unpriced (e.g. Audi A8).
        if any(re.search(r"AED\s*[\d,]+|\bcc\b|\bhp\b|Compare|View Offers", " ".join(r), re.I) for r in rows):
            table = t
            break

    for row in _table_rows(table):
        if len(row) < 2:
            continue
        name = row[0]
        row_str = " ".join(row)
        price_m = re.search(r"AED\s*([\d,]+)", row_str)
        spec_summary = row[2] if len(row) > 2 else ""
        url = row[-1] if row and row[-1].startswith("http") else ""
        
        if not name:
            continue
            
        price_aed = price_m.group(1).replace(",", "") if price_m else ""
        price_display = f"AED {price_m.group(1)}" if price_m else "TBA"

        engine_m = re.search(r"([\d,]+)\s*cc", spec_summary, re.I)
        power_m = re.search(r"(\d+\s*Hp[^,]*)", spec_summary, re.I)
        variants.append({
            "variant_name": name,
            "variant_url": url,
            "price_aed": price_aed,
            "price_display": price_display,
            "transmission": "Automatic" if re.search(r"automatic", spec_summary, re.I)
                             else ("Manual" if re.search(r"manual", spec_summary, re.I) else ""),
            "engine_cc": engine_m.group(1).replace(",", "") if engine_m else "",
            "power": _clean_ws(power_m.group(1)) if power_m else "",
            "spec_summary": spec_summary,
            "specifications": [],
            "features": [],
        })
    return variants


def _extract_colours(soup: BeautifulSoup, brand: str, model: str) -> list[dict]:
    """Pairs each .../color/ image with its colour name (from alt text, with
    the 'Brand Model ' prefix stripped)."""
    colours = []
    seen = set()
    prefix_rx = re.compile(rf"^\s*{re.escape(brand)}\s+{re.escape(model)}\s*", re.I) if brand and model else None
    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "")
        if "/gallery/color/" not in src:
            continue
        alt = _clean_ws(img.get("alt", ""))
        if prefix_rx:
            alt = prefix_rx.sub("", alt)
        if not alt or alt in seen:
            continue
        seen.add(alt)
        colours.append({"name": alt, "image_url": src.split("?")[0]})
    return colours


def _extract_gallery_images(html: str) -> dict:
    """Categorised gallery image URLs (exterior / interior / color), deduped,
    upgraded to the largest available CDN size (?tr=w-930)."""
    buckets: dict[str, list[str]] = {"exterior": [], "interior": [], "color": []}
    seen = set()
    for m in IMG_CDN_RE.finditer(html):
        category, brand_id, model_id, filename = m.groups()
        base_url = f"https://imagecdn.zigwheels.ae/large/gallery/{category}/{brand_id}/{model_id}/{filename}"
        if base_url in seen:
            continue
        seen.add(base_url)
        buckets[category.lower()].append(base_url + "?tr=w-930")
    return buckets


# FIX (2026-08-17): the /specifications sub-page's real, fully-categorized
# spec tables (Engine and Transmission, Performance, Dimensions & Capacity,
# Suspension, Steering, Brakes & Tyres, Comfort & Convenience, Entertainment
# & Communication, Exterior, Interior, Safety and Security, Miscellaneous —
# the exact 12-category structure zigwheels.ae itself uses) load via
# client-side JS/AJAX. A plain requests+BeautifulSoup fetch (used everywhere
# else in this scraper) only ever sees the intro paragraph — confirmed by
# hand: curl/requests on that URL returns no <table>/<dl>/spec markup at
# all, JS included. The Review-accordion + FAQ-mining fallback used until
# now genuinely can't recover this — the source text for it just doesn't
# exist outside the JS-rendered page for many models.
#
# This function uses a headless browser (Playwright) to render that page for
# real and then tries, in order:
#   1) Sniff the page's own AJAX/XHR JSON responses for the spec payload
#      (most robust — no CSS guessing, works even if the front-end markup
#      changes, and captures the *exact* category names zigwheels uses).
#   2) Fall back to parsing the fully-rendered DOM for heading + table/list
#      pairs if no matching JSON response was seen (e.g. data was inlined
#      into a hydration script instead of a separate XHR call).
#
# REQUIRES: `pip install playwright --break-system-packages && playwright
# install chromium` on the machine that actually runs the scraper (not
# testable in a sandboxed/offline environment). If Playwright isn't
# installed, this silently returns [] and the caller falls back to the
# existing accordion/FAQ sources — nothing else in the pipeline breaks.
#
# KNOWN LIMITATION: category/spec-name label text is guessed generically
# (known label list below) since the real rendered DOM/JSON shape couldn't
# be inspected while writing this (no network access in the dev sandbox).
# Run once with SCRAPER_DEBUG_SPEC_JSON=1 (dumps every intercepted JSON
# response to ./debug_spec_responses/) and send me that dump if step 1
# below finds nothing useful for a given model — I'll tighten the parser to
# match the real payload shape exactly.
_SPEC_PAGE_CATEGORY_LABELS = [
    "Engine and Transmission", "Engine & Transmission", "Performance",
    "Dimensions & Capacity", "Dimensions and Capacity",
    "Suspension", "Steering", "Brakes & Tyres", "Brakes and Tyres",
    "Comfort & Convenience", "Comfort and Convenience",
    "Entertainment & Communication", "Entertainment and Communication",
    "Exterior", "Interior", "Safety and Security", "Safety & Security",
    "Miscellaneous",
]


def _extract_specifications_page_js(url: str) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    Render {model_url}/specifications with a headless browser and pull the
    fully categorized spec/feature list out of it — for EVERY variant tab,
    not just whichever one happens to be active by default.

    zigwheels.ae's /specifications page has a row of variant tabs:

        <ul id="specsTabbing">
          <li data-variantid="11794" class="variantSpecification ... active">1.5L Smart ...</li>
          <li data-variantid="11795" class="variantSpecification ...">1.5L Comfort ...</li>
          <li data-variantid="12649" class="variantSpecification ...">1.6L Smart Plus Hev ...</li>
        </ul>
        <div id="variantSpecification-11794">  <!-- ACTIVE tab: server-rendered, full accordions -->
        <div id="variantSpecification-11795">  <!-- inactive tab: EMPTY until clicked -->
        <div id="variantSpecification-12649">  <!-- inactive tab: EMPTY until clicked -->

    BUG FIXED (2026-08-17): the old version only ever read the page once, so
    it only ever saw the active tab's accordions (whatever variant zigwheels
    happened to default to — often the base petrol trim) and returned ONE
    flat list. That list then got reused as "the" spec set for every variant
    of the model, which is exactly why a Hybrid/EV variant's own fields
    (Battery Capacity, Electric Motor Power, regen braking, the differing
    Miscellaneous rows, etc.) never showed up — that tab's HTML was never
    populated in the first place, let alone scraped.

    Now: for each ``data-variantid`` tab, click it (unless it's already the
    active one), wait for its container to populate, and parse it in
    isolation. Works generically for hybrid AND electric models too — the
    category labels aren't hardcoded, whatever ``acc-title`` headings that
    variant's accordion actually has (e.g. an EV's "Battery, Range &
    Charging" tab) get picked up the same way an ICE variant's "Engine and
    Transmission" tab does.

    Returns
    -------
    (model_specs, variant_specs_by_name)
      model_specs         — flat [{"category","name","value"}, ...] list for
                             whichever tab is active by default (same shape/
                             behaviour as before, for backward compatibility
                             with callers that only want "the" model spec).
      variant_specs_by_name — {variant_tab_name: [{"category","name","value"}, ...]}
                             one real, independently-scraped spec list per
                             variant tab, keyed by the tab's own label text
                             (e.g. "1.6L Smart Plus Hev"). Empty dict if the
                             page has no variant tabs at all (older/simpler
                             layouts) or Playwright isn't available.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("  Playwright not installed — skipping JS-rendered /specifications "
                  "fetch (pip install playwright && playwright install chromium). "
                  "Falling back to Review-accordion/FAQ sources only.")
        return [], {}

    spec_url = url.rstrip("/") + "/specifications"
    debug_dir = os.environ.get("SCRAPER_DEBUG_SPEC_JSON")
    captured_json: list[dict] = []

    def _on_response(response):
        try:
            ctype = response.headers.get("content-type", "")
            if "application/json" not in ctype:
                return
            body = response.json()
        except Exception:
            return
        blob = json.dumps(body).lower()
        if any(lbl.lower() in blob for lbl in _SPEC_PAGE_CATEGORY_LABELS):
            captured_json.append({"url": response.url, "body": body})

    model_specs: list[dict] = []
    variant_specs_by_name: dict[str, list[dict]] = {}
    default_rendered_html = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
            page.on("response", _on_response)
            page.goto(spec_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            default_rendered_html = page.content()

            # Discover the variant tabs (data-variantid + label text + which
            # one is active). If there are none, this is an older/simpler
            # layout — fall through to the old single-shot behaviour below.
            try:
                tabs = page.eval_on_selector_all(
                    "li[data-variantid]",
                    "els => els.map(el => ({"
                    "  id: el.getAttribute('data-variantid'),"
                    "  name: el.childNodes[0] && el.childNodes[0].textContent "
                    "        ? el.childNodes[0].textContent.trim() : el.innerText.trim(),"
                    "  active: el.classList.contains('active')"
                    "}))",
                )
            except Exception as exc:
                log.warning(f"  Could not read variant tab list on {spec_url}: {exc}")
                tabs = []

            for tab in tabs:
                vid, vname, is_active = tab.get("id"), _clean_ws(tab.get("name") or ""), tab.get("active")
                if not vid or not vname:
                    continue
                if not is_active:
                    try:
                        page.click(f'li[data-variantid="{vid}"]')
                        page.wait_for_selector(
                            f'#variantSpecification-{vid} .acc-title', timeout=15000)
                        page.wait_for_timeout(600)
                    except Exception as exc:
                        log.warning(f"  Timed out waiting for variant tab '{vname}' "
                                    f"({vid}) to load on {spec_url}: {exc}")
                        continue
                try:
                    panel_html = page.eval_on_selector(
                        f'#variantSpecification-{vid}', "el => el.outerHTML")
                except Exception as exc:
                    log.warning(f"  Could not read panel for variant '{vname}' ({vid}): {exc}")
                    continue
                panel_soup = BeautifulSoup(panel_html, "html.parser")
                v_specs = _parse_rendered_spec_dom(panel_soup)
                if v_specs:
                    variant_specs_by_name[vname] = v_specs
                    log.info(f"  Got {len(v_specs)} categorized specs for variant "
                              f"'{vname}' ({vid}) from JS-rendered /specifications page")
                if is_active:
                    model_specs = v_specs

            browser.close()
    except Exception as exc:
        log.warning(f"  Playwright fetch of {spec_url} failed: {exc}")
        return [], {}

    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(captured_json):
            fname = Path(debug_dir) / f"{re.sub(r'[^a-z0-9]+', '_', url.lower())}_{i}.json"
            fname.write_text(json.dumps(item, indent=2), encoding="utf-8")
        if default_rendered_html:
            (Path(debug_dir) / "rendered.html").write_text(default_rendered_html, encoding="utf-8")
        log.info(f"  Wrote {len(captured_json)} intercepted JSON response(s) + rendered "
                  f"HTML to {debug_dir}/ for inspection")

    # --- Fallback: no variant tabs found at all — old single-shot behaviour.
    if not variant_specs_by_name:
        for item in captured_json:
            results = _parse_spec_json_payload(item["body"])
            if results:
                log.info(f"  Got {len(results)} categorized specs from JS payload: {item['url']}")
                return results, {}
        rendered_soup = BeautifulSoup(default_rendered_html, "html.parser")
        results = _parse_rendered_spec_dom(rendered_soup)
        if results:
            log.info(f"  Got {len(results)} categorized specs from rendered DOM (single tab / no variant tabs)")
        else:
            log.warning(f"  JS-rendered /specifications page produced no recognisable spec "
                        f"data for {url} — set SCRAPER_DEBUG_SPEC_JSON=1 and re-run to dump "
                        f"the raw payload for review")
        return results, {}

    if not model_specs:
        # Active tab's own parse failed for some reason — fall back to the
        # union of whatever variants DID parse, so callers never get an
        # empty model-level spec list when variant data clearly exists.
        seen = set()
        for specs in variant_specs_by_name.values():
            for s in specs:
                key = (s["category"], s["name"])
                if key not in seen:
                    seen.add(key)
                    model_specs.append(s)

    return model_specs, variant_specs_by_name


def _parse_spec_json_payload(body, _category: str | None = None) -> list[dict]:
    """
    Recursively walk an arbitrary JSON structure looking for
    {label/name/title: ..., value/spec_value: ...} pairs nested under one of
    the known category labels. Shape is guessed generically since the real
    API response couldn't be inspected offline — see module-level note above.
    """
    out: list[dict] = []

    def walk(node, category):
        if isinstance(node, dict):
            # A category heading node: {"title": "Engine and Transmission", "items": [...]}
            label = node.get("title") or node.get("category") or node.get("heading") or node.get("name")
            if isinstance(label, str) and label.strip() in _SPEC_PAGE_CATEGORY_LABELS:
                category = label.strip()
            # A single spec-row node: {"label": "Max Power", "value": "113 hp"}
            row_name = node.get("label") or node.get("specName") or node.get("key")
            row_value = node.get("value") or node.get("specValue")
            if category and isinstance(row_name, str) and isinstance(row_value, (str, int, float)):
                out.append({"category": category, "name": _clean_ws(row_name),
                            "value": _clean_ws(str(row_value))})
            for v in node.values():
                walk(v, category)
        elif isinstance(node, list):
            for v in node:
                walk(v, category)

    walk(body, _category)
    return out


def _parse_rendered_spec_dom(soup) -> list[dict]:
    """
    Parse the fully-rendered /specifications page — or a SCOPED fragment of
    it (see below).

    CONFIRMED (2026-08-17) against a real rendered.html dump — zigwheels.ae's
    page uses this exact structure per category:

        <div class="shadow-light card accordion ...">
          <div class="acc-title bg-grey ajax-data">Engine and Transmission</div>
          <div class="acc-panel" id="variantSpecification">
            <table><tbody>
              <tr>
                <td class="label-spec"><span>Transmission Type</span></td>
                <td class="value-spec"><div class="read-more" ...><div>Automatic</div></div></td>
              </tr>
              ...
            </tbody></table>
          </div>
        </div>

    There are 10 accordion sections in total (not 12 — "Suspension", "Steering"
    and "Brakes & Tyres" turned out to be ONE combined heading:
    "Suspension, Steering , Brakes & Tyres", which already routes correctly
    into CATEGORY_TO_GROUP's "suspension"/"steering"/"brakes & tyres" fuzzy
    matches via substring containment). Verified end-to-end on the Elantra:
    95 real spec/feature rows recovered across all 10 categories, vs. ~40-50
    from the Review-accordion fallback alone — including previously-missing
    ones like Acceleration, Cargo Volume, Front/Rear Suspension type, and the
    full Comfort/Entertainment/Safety feature lists.

    IMPORTANT (2026-08-17, variant-tab bug fix): the page only server-renders
    the accordion for the CURRENTLY ACTIVE variant tab
    (``<div id="variantSpecification-{variantid}">…</div>``) — the other
    variant tabs exist as EMPTY sibling containers that only get their
    accordion HTML injected client-side, after you click that tab. Calling
    this on the whole page only ever finds the active tab's accordions,
    which is why every variant used to come out with IDENTICAL specs (always
    whichever variant happened to be active/default) — a Hybrid or EV
    variant's battery/motor/Miscellaneous rows were silently lost whenever a
    plain petrol trim was the active tab.

    Pass ``soup`` as either:
      - the full page/soup (legacy behaviour — only the active tab's specs), or
      - a SCOPED element for one variant's own
        ``#variantSpecification-{variantid}`` container (new — see
        ``_extract_specifications_page_js``), so each variant's real specs
        are captured separately instead of every variant sharing tab #1's data.
    """
    out: list[dict] = []
    for title_div in soup.find_all("div", class_="acc-title"):
        label = _clean_ws(title_div.get_text(" ", strip=True))
        if not label or label.lower() == "compare":
            continue
        panel = title_div.find_next_sibling("div", class_="acc-panel")
        if not panel:
            continue
        for row in panel.find_all("tr"):
            name_td = row.find("td", class_="label-spec")
            val_td = row.find("td", class_="value-spec")
            if not name_td or not val_td:
                continue
            name = _clean_ws(name_td.get_text(" ", strip=True))
            value = _clean_ws(val_td.get_text(" ", strip=True))
            if name and value:
                out.append({"category": label, "name": name, "value": value})
    return out


def _extract_categorized_specs(soup: BeautifulSoup) -> list[dict]:
    """
    Extracts categorized specs from the "<Model> Review" accordion.
    Updated to extract exact numbers for seating capacity (e.g., "5 Seater" -> "5").
    """
    review_heading = _find_heading(soup, r"Review$")
    if review_heading is None:
        return []

    section = _elements_between(
        soup, review_heading,
        [r"^Gallery of", r"^User Reviews of", r"Questions & Answers", r"^Compare Variants Of"],
    )

    results = []
    current_category = ""
    seen = set()
    seen_tables = set()
    
    for tag in section:
        name = getattr(tag, "name", None)
        if name in ("h2", "h3", "h4"):
            current_category = _clean_ws(tag.get_text(" ", strip=True))
            continue

        if name == "table":
            if id(tag) in seen_tables or not current_category:
                continue
            seen_tables.add(id(tag))
            rows = tag.find_all("tr")
            if not rows:
                continue
            for tr in rows[1:]:
                cells = [_clean_ws(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if len(cells) < 2:
                    continue
                spec_name, val = cells[0], cells[1]
                if not spec_name or not val or val in ("-", "—"):
                    continue
                if len(spec_name) > 80 or len(val) > 150:
                    continue
                key = (current_category, spec_name)
                if key in seen:
                    continue
                seen.add(key)
                results.append({"category": current_category, "name": spec_name, "value": val})
            continue

        if name != "li" or not current_category:
            continue
            
        txt = _clean_ws(tag.get_text(" ", strip=True))
        if not txt or len(txt) > 220:
            continue
        if txt.lower() in ("read more", "view more", "key highlights"):
            continue

        if ":" in txt:
            label, _, val = txt.partition(":")
            label, val = _clean_ws(label), _clean_ws(val)
            if label and val:
                key = (current_category, label)
                if key not in seen:
                    seen.add(key)
                    results.append({"category": current_category, "name": label, "value": val})
        else:
            word_count = len(txt.split())
            looks_like_sentence = txt.endswith(".") and word_count > 6
            key = (current_category, txt)
            
            # --- NEW: Seating Capacity Extraction ---
            m_seater = re.match(r"^(\d+)\s*[- ]?Seater$", txt, re.I)
            
            if m_seater:
                seat_key = (current_category, "Seating Capacity")
                if seat_key not in seen:
                    seen.add(seat_key)
                    # Use the captured number instead of "Yes"
                    results.append({"category": current_category, "name": "Seating Capacity", "value": m_seater.group(1)})
                    
            elif not looks_like_sentence and key not in seen and 3 < len(txt) < 120:
                seen.add(key)
                results.append({"category": current_category, "name": txt, "value": "Yes"})

    return results

# Review-accordion tab headings, in the order they appear on the page, used
# both to bound each tab's content and (for _extract_review_narrative) to
# know which heading starts the *next* tab so we stop collecting paragraphs
# at the right place.
REVIEW_TAB_HEADINGS = [
    "Summary", "Pros & Cons", "Interior", "Safety", "Exterior",
    r"Engine\s*&?\s*Fuel Consumption", "Features", "Variant", "Verdict",
]


def _extract_review_narrative(soup: BeautifulSoup) -> dict:
    """
    Pulls the descriptive/narrative paragraph text (NOT the bulleted
    label:value spec rows — see _extract_categorized_specs for those) out of
    the "<Model> Review" accordion, keyed by tab, for the What's New section:

      introduction <- "Summary" tab
      exterior     <- "Exterior" tab
      interior     <- "Interior" tab
      powertrain   <- "Engine & Fuel Consumption" tab

    Returns {"introduction": [str, ...], "exterior": [...], "interior": [...],
    "powertrain": [...]} — each a list of paragraph-level strings (usually
    1-4 per tab) suitable for a What's New repeater.
    """
    review_heading = _find_heading(soup, r"Review$")
    if review_heading is None:
        return {"introduction": [], "exterior": [], "interior": [], "powertrain": []}

    tab_rx = re.compile("|".join(rf"^{p}$" for p in REVIEW_TAB_HEADINGS), re.I)
    all_tags = list(soup.descendants)
    try:
        start_idx = all_tags.index(review_heading)
    except ValueError:
        return {"introduction": [], "exterior": [], "interior": [], "powertrain": []}

    end_rx = re.compile(r"^Gallery of|^User Reviews of|Questions & Answers|^Compare Variants Of", re.I)

    tab_key_by_heading = {
        "summary": "introduction",
        "exterior": "exterior",
        "interior": "interior",
    }

    out: dict[str, list[str]] = {"introduction": [], "exterior": [], "interior": [], "powertrain": []}
    current_key: str | None = None
    seen: set[str] = set()

    for i in range(start_idx + 1, len(all_tags)):
        tag = all_tags[i]
        name = getattr(tag, "name", None)
        if name is None:
            continue
        txt = _clean_ws(tag.get_text(" ", strip=True)) if hasattr(tag, "get_text") else ""

        if name in ("h2",) and end_rx.search(txt):
            break

        if name in ("h3", "h4") and tab_rx.match(txt):
            norm = txt.lower().strip()
            if norm.startswith("engine") and "fuel" in norm:
                current_key = "powertrain"
            else:
                current_key = tab_key_by_heading.get(norm)
            continue

        if current_key is None:
            continue
        # FIX (2026-08-13): some cars (e.g. Elantra) write the "Engine &
        # Fuel Consumption"/other tabs' narrative as <li> bullets under a
        # bold sub-heading ("City Performance", "Highway Performance", ...)
        # instead of <p> paragraphs like Creta/Tucson use — accept both
        # shapes so we don't silently end up with an empty list for cars
        # formatted this way.
        if name not in ("p", "li"):
            continue
        # Skip nested/heading-bearing wrappers, "Key Highlights" labels, and
        # short throwaway fragments (image captions, "Read More", etc).
        if not txt or txt.lower() in ("key highlights", "read more", "read less"):
            continue
        if len(txt) < 40:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        out[current_key].append(txt)

    return out


def _elements_between(soup: BeautifulSoup, start_tag, end_patterns: list[str]) -> list:
    """All descendant tags between start_tag (exclusive) and the next h2
    matching one of end_patterns (exclusive)."""
    all_tags = list(soup.descendants)
    try:
        start_idx = all_tags.index(start_tag)
    except ValueError:
        return []
    end_rx = re.compile("|".join(end_patterns), re.I)
    end_idx = len(all_tags)
    for i in range(start_idx + 1, len(all_tags)):
        t = all_tags[i]
        if getattr(t, "name", None) == "h2" and end_rx.search(t.get_text(" ", strip=True)):
            end_idx = i
            break
    return all_tags[start_idx + 1:end_idx]


def _extract_variant_spec_table(soup: BeautifulSoup) -> dict[str, list[dict]]:
    """
    Parses the real variant-wise spec comparison table inside the review
    accordion's 'Variant' tab — e.g.:
        | Feature | Smart Pro | Premium |
        | Wheels  | 17" Alloy | 17" Diamond-Cut Alloy |
    Returns {variant_column_name: [{"name": spec, "value": val}, …]}.
    """
    table = None
    fallback = None
    for t in soup.find_all("table"):
        first_row = t.find("tr")
        if not first_row:
            continue
        header_cells = [_clean_ws(c.get_text(" ", strip=True)) for c in first_row.find_all(["th", "td"])]
        if not header_cells:
            continue
        head0 = header_cells[0].lower()
        if head0.startswith("specification"):
            table = t
            break
        if head0.startswith("feature") and fallback is None:
            fallback = t
    if table is None:
        table = fallback
    if table is None:
        return {}

    rows = table.find_all("tr")
    if not rows:
        return {}
    header_cells = [_clean_ws(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"])]
    variant_names = header_cells[1:]
    matrix: dict[str, list[dict]] = {name: [] for name in variant_names if name}

    for tr in rows[1:]:
        cells = [_clean_ws(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        spec_name = cells[0]
        if not spec_name:
            continue
        for i, val in enumerate(cells[1:]):
            if i >= len(variant_names) or not variant_names[i] or not val or val == "-":
                continue
            matrix[variant_names[i]].append({"name": spec_name, "value": val})

    return matrix


def _extract_variant_feature_lists(soup: BeautifulSoup) -> dict[str, list[str]]:
    """
    Parses the 'Compare Variants Of <Model>' section: each variant is shown
    as a card — variant name, price, then either 'Base Variant Features'
    (full list) or '+N Additional Feature(s)' (delta list only vs. the
    variant(s) before it). Returns the RAW per-card lists (not yet merged —
    see _merge_variant_features()).
    """
    heading = _find_heading(soup, r"^Compare Variants Of")
    if heading is None:
        return {}

    full_text = soup.get_text("\n", strip=True)
    h_text = heading.get_text(" ", strip=True)
    start = full_text.find(h_text)
    if start == -1:
        return {}

    end_m = re.search(r"\nCompare .* With Similar Cars", full_text[start:])
    section = full_text[start:start + end_m.start()] if end_m else full_text[start:start + 8000]

    marker_rx = re.compile(
        r"([A-Za-z0-9][\w .()\-/]{2,60}?)\n\s*AED\s*[\d,]+\s*Price\s*"
        r"(Base Variant Features|\+\s*\d+\s*Additional Feature\(s\))?",
        re.I,
    )
    matches = list(marker_rx.finditer(section))
    results: dict[str, list[str]] = {}
    for i, m in enumerate(matches):
        variant_name = _clean_ws(m.group(1))
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
        block = section[block_start:block_end]
        items = []
        for line in block.split("\n"):
            line = _clean_ws(line)
            if not line or len(line) > 60:
                continue
            if line.lower() in ("view more", "read more", "read less"):
                continue
            items.append(line)
        if variant_name:
            results[variant_name] = items
    return results


def _merge_variant_features(raw_lists: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    zigwheels.ae only lists NEW features for every variant after the base
    one ('+9 Additional Feature(s)'). Assumes cards are in ascending trim
    order (matches price ordering on the page) and accumulates so each
    variant's list is its FULL feature set, not just the delta.
    """
    merged: dict[str, list[str]] = {}
    cumulative: list[str] = []
    for name, feats in raw_lists.items():
        for f in feats:
            if f not in cumulative:
                cumulative.append(f)
        merged[name] = list(cumulative)
    return merged


def _norm_variant_key(name: str, model_name: str = "") -> str:
    n = (name or "").lower()
    if model_name:
        n = n.replace(model_name.lower(), "")
    return re.sub(r"[^a-z0-9]+", "", n)


def _match_variant_bucket(target_norm: str, norm_map: dict[str, list]) -> list:
    if not target_norm:
        return []
    for cand_norm, val in norm_map.items():
        if cand_norm and (target_norm in cand_norm or cand_norm in target_norm):
            return val
    return []


def _extract_faqs(soup: BeautifulSoup) -> list[dict]:
    """'Q. ...' headings followed by 'A. ...' answer text."""
    faqs = []
    for h in soup.find_all(["h3", "h4"]):
        q_text = _clean_ws(h.get_text(" ", strip=True))
        if not q_text.lower().startswith("q."):
            continue
        question = _clean_ws(q_text[2:])
        # Answer is the next block of text starting with "A" / "A."
        ans_node = h.find_next(string=re.compile(r"^\s*A[.\s]"))
        answer = ""
        if ans_node:
            answer = _clean_ws(str(ans_node))
            answer = re.sub(r"^A\.?\s*", "", answer)
        if question:
            faqs.append({"question": question, "answer": answer})
    return faqs


def _extract_specs_from_faqs(faqs: list[dict]) -> list[dict]:
    """
    FALLBACK spec source for models whose overview page has no "<Model>
    Review" accordion (no Core Dimensions / Engine Specification / Safety
    sub-headings for _extract_categorized_specs() to walk — this happens
    for thinly-populated / newer listings, e.g. Stargazer). The real spec
    numbers are usually still on the page, just buried as prose inside the
    FAQ answers (dimensions, weight, fuel tank, cargo, power/torque,
    engine cc, airbags, ABS, and a full "safety features are: A, B, C"
    sentence). Mine those with regex into the same
    [{"category", "name", "value"}, …] shape _extract_categorized_specs()
    produces, so it drops straight into acf_mapper.py's existing matching
    (category is only a tiebreak there — spec *name* is what's matched
    against the ACF choice dictionaries).

    Only called when the accordion-based extraction comes back empty —
    it never overrides real categorized spec data.
    """
    specs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, name: str, value: str):
        value = _clean_ws(value)
        if not value:
            return
        key = (category, _normalise_faq_spec(name))
        if key in seen:
            return
        seen.add(key)
        specs.append({"category": category, "name": name, "value": value})

    for item in faqs or []:
        q = item.get("question", "") or ""
        a = item.get("answer", "") or ""
        if not a:
            continue

        # "The Dimensions... Length: 4460mm Width: 1780mm Height: 1690mm WheelBase: 2780mm"
        m = re.search(
            r"Length:\s*([\d,]+\s*mm).*?Width:\s*([\d,]+\s*mm).*?Height:\s*([\d,]+\s*mm).*?"
            r"Wheel\s*Base:\s*([\d,]+\s*mm)", a, re.I | re.S)
        if m:
            add("Dimensions & Weight", "Length", m.group(1))
            add("Dimensions & Weight", "Width", m.group(2))
            add("Dimensions & Weight", "Height", m.group(3))
            add("Dimensions & Weight", "Wheelbase", m.group(4))
        else:
            # "The length of X is 4460 mm, while the width is 1780 mm."
            m = re.search(r"length of .*?\bis\s*([\d,]+\s*mm).*?width is\s*([\d,]+\s*mm)", a, re.I)
            if m:
                add("Dimensions & Weight", "Length", m.group(1))
                add("Dimensions & Weight", "Width", m.group(2))

        # "...fuel tank capacity of X is 40 L."
        m = re.search(r"fuel tank capacity of .*?\bis\s*([\d.]+\s*L\b)", a, re.I)
        if m:
            add("Capacity", "Fuel Tank Capacity", m.group(1))

        # "...has a gross weight of 1830 kg." / "kerb weight of 1830 kg"
        m = re.search(r"(?:gross|kerb)\s+weight of\s*([\d,]+\s*kg)", a, re.I)
        if m:
            add("Dimensions & Weight", "Kerb Weight", m.group(1))

        # "...has Cargo volume of 200 L." / "cargo capacity ... 200 L"
        m = re.search(r"[Cc]argo (?:volume|capacity)(?: of)?\s*(?:is\s*)?([\d,]+\s*L\b)", a, re.I)
        if m:
            add("Capacity", "Boot Space", m.group(1))

        # "...delivers 113Hp@6300rpm of maximum power and 144Nm@4500rpm of maximum torque."
        m = re.search(
            r"([\d.]+\s*Hp[^,]*?)\s*of maximum power and\s*([\d.]+\s*Nm[^,.]*?)\s*of maximum torque",
            a, re.I)
        if m:
            add("Engine & Performance", "Max Power", m.group(1))
            add("Engine & Performance", "Max Torque", m.group(2))

        # "...offered in 1 engine option: 1497 cc." (only from an engine-capacity FAQ)
        if re.search(r"engine (option|capacity)", q, re.I):
            m = re.search(r"([\d,]+)\s*cc\b", a, re.I)
            if m:
                add("Engine & Performance", "Displacement", f"{m.group(1)} cc")

        # "Does X have Airbags? Yes, driver airbag, passenger airbag."
        if re.search(r"\bairbags?\b", q, re.I) and re.match(r"\s*yes\b", a, re.I):
            names = re.findall(
                r"(driver airbag|passenger airbag|rear side airbag|side airbag|"
                r"curtain airbag|knee airbag)", a, re.I)
            uniq = list(dict.fromkeys(n.lower() for n in names))
            for n in uniq:
                add("Safety & Security", n.title(), "Yes")
            if uniq:
                add("Safety & Security", "No. of Airbags", str(len(uniq)))

        # "Does X have Anti Lock Braking System? Yes, ..."
        if re.search(r"anti lock braking system", q, re.I) and re.match(r"\s*yes\b", a, re.I):
            add("Safety & Security", "ABS", "Yes")

        # "<Model> safety features are: Central Locking, Passenger Airbag, ... and Fire Extinguisher"
        m = re.search(r"safety features are:?\s*(.+)", a, re.I)
        if m:
            tail = re.split(r"\bHelpful\b", m.group(1))[0]
            tail = tail.rstrip(". ")
            for part in re.split(r",|\band\b", tail):
                name = _clean_ws(part)
                if name and 2 < len(name) < 60:
                    add("Safety & Security", name, "Yes")

        # FIX (2026-08-13) — Bug 13: zigwheels.ae FAQ pages routinely answer
        # tyre size / ground clearance / suspension / brake / warranty
        # questions even when the Review accordion (the main source for
        # _extract_categorized_specs) says nothing about them at all — this
        # was the direct cause of Suspension/Steering/Brakes & Tyres/
        # Miscellaneous coming through completely empty on the live site for
        # models like the Elantra, whose accordion text just never mentions
        # them. These patterns mine the same FAQ answers already being
        # fetched for the safety-feature/dimension patterns above, so no
        # extra HTTP requests are needed.

        # "...tyre size of X is 205/55 R16." / "...comes with 215/45 R17 tyres."
        m = re.search(r"tyre size[^.]*?\bis\s*([\d]{3}\s*/\s*\d{2}\s*R\s*\d{2})", a, re.I)
        if not m:
            m = re.search(r"\b(\d{3}\s*/\s*\d{2}\s*R\s*\d{2})\b.{0,20}tyres?\b", a, re.I)
        if m:
            add("Brakes & Tyres", "Tyre Size", _clean_ws(m.group(1)).upper())

        # "...ground clearance of X is 190 mm."
        m = re.search(r"ground clearance[^.]*?\bis\s*([\d,]+\s*mm)", a, re.I)
        if m:
            add("Dimensions & Capacity", "Ground Clearance", m.group(1))

        # "X has front disc brakes and rear drum brakes." (also matches "disc-disc")
        m = re.search(r"\bfront\s+(disc|drum)\s+brakes?\b", a, re.I)
        if m:
            add("Brakes & Tyres", "Front Brake Type", f"{m.group(1).title()} Brakes")
        m = re.search(r"\brear\s+(disc|drum)\s+brakes?\b", a, re.I)
        if m:
            add("Brakes & Tyres", "Rear Brake Type", f"{m.group(1).title()} Brakes")

        # "X uses MacPherson strut front suspension and torsion beam rear suspension."
        m = re.search(r"(mcpherson|macpherson)[^.]{0,20}\bfront\s+suspension", a, re.I)
        if m:
            add("Suspension", "Front Suspension", "MacPherson Strut")
        m = re.search(r"torsion\s*beam[^.]{0,20}\brear\s+suspension", a, re.I)
        if m:
            add("Suspension", "Rear Suspension", "Torsion Beam")
        m = re.search(r"multi[- ]link[^.]{0,20}\brear\s+suspension", a, re.I)
        if m:
            add("Suspension", "Rear Suspension", "Multi-Link")

        # "X comes with a manufacturer warranty of 5 years / 100,000 km."
        m = re.search(
            r"warranty of\s*(\d+)\s*years?\s*/?\s*([\d,]+\s*km)?", a, re.I)
        if m and re.search(r"\bwarranty\b", q, re.I):
            years = m.group(1)
            km = m.group(2)
            val = f"{years} Years" + (f" / {km}" if km else "")
            add("Miscellaneous", "Manufacturer Warranty", val)

        # "X is offered in 5 doors." / "5-door hatchback"
        m = re.search(r"\b(\d)[- ]doors?\b", a, re.I)
        if m and re.search(r"\bdoors?\b", q, re.I):
            add("Miscellaneous", "No. of Doors", m.group(1))

    return specs


def _normalise_faq_spec(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _normalise_category(category: str) -> str:
    cat = str(category or "").strip().lower()
    cat = cat.replace("&", " and ")
    cat = re.sub(r"\s*\([^)]*\)", "", cat)
    cat = cat.split(":", 1)[0]
    return re.sub(r"\s+", " ", cat).strip()


# FIX (2026-08-13) — Bug 11: the "cars-data" model post's Cars Overview
# group has body_type / fuel_type ACF fields, but nothing in this scraper
# ever populated them:
#   - body_type: there was no extraction function for it at all.
#   - fuel_type: _extract_key_specs() only reads whatever the page's own
#     "Key Specs of <Model>" box happens to show, and that box does not
#     always include a "Fuel Type" row (e.g. Hyundai Elantra's box only has
#     Transmission/Drive Type/Engine/Power/Torque/Battery Capacity/Airbag —
#     no Fuel Type row at all), so the field silently stayed empty even
#     though the page clearly states the car is Petrol/Hybrid elsewhere
#     (variant feature flags, "Engine & Fuel Consumption" spec table).
# Both fields are now inferred from data the scraper already collects.

_BODY_TYPE_WORDS = [
    "Hatchback", "Sedan", "Coupe", "Convertible", "Pickup", "Crossover",
    "Wagon", "MPV", "Minivan", "Van", "SUV",
]


def _infer_body_type(result: dict) -> str:
    """
    zigwheels.ae doesn't render an explicit 'Body Type: Sedan' field on the
    overview page — it's only implied in the review prose ("...C-segment
    compact sedan...", "...SUV...") and site navigation. Scan the review
    narrative + intro paragraphs for the first recognised body-type word.
    Order matters: check the more specific "Crossover"/"Pickup"/etc. before
    the very generic "SUV" would otherwise dominate.
    """
    haystacks = [
        result.get("price_overview_text", ""),
        result.get("spec_overview_text", ""),
    ]
    narrative = result.get("review_narrative") or {}
    for bucket in ("introduction", "exterior", "interior"):
        haystacks.extend(narrative.get(bucket) or [])

    text = " ".join(h for h in haystacks if h)
    for word in _BODY_TYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text, re.I):
            return word
    return ""


_FUEL_TYPE_WORDS = ["Electric", "Hybrid", "Plug-in Hybrid", "Diesel", "Petrol", "CNG"]


def _infer_fuel_type(result: dict) -> str:
    """
    Same gap as body type, but for fuel type: the Key Specs box doesn't
    always carry a 'Fuel Type' row, even though every variant's feature
    list on the 'Compare Variants' table explicitly flags Petrol/Hybrid/
    Diesel/Electric as a boolean feature. Collect the distinct fuel types
    actually present across all scraped variants instead.
    """
    found: list[str] = []
    for v in result.get("variants", []):
        for f in v.get("features") or []:
            name = str(f.get("name", "")).strip()
            for word in _FUEL_TYPE_WORDS:
                if name.lower() == word.lower() and word not in found:
                    found.append(word)
    if found:
        return ", ".join(found)

    # Fallback: scan the review narrative for an explicit fuel-type mention.
    narrative = result.get("review_narrative") or {}
    text = " ".join(narrative.get("powertrain") or narrative.get("introduction") or [])
    for word in _FUEL_TYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text, re.I):
            return word
    return ""


def _extract_similar_cars(soup: BeautifulSoup) -> list[dict]:
    table = _find_table_after(soup, r"Compare .* With Similar Cars")
    if table is None:
        return []
    cars = []
    for row in _table_rows(table):
        text = " ".join(row)
        if re.search(r"currently viewing", text, re.I):
            continue
        m = re.search(r"AED\s*[\d,]+(?:\s*-\s*AED?\s*[\d,]+)?", text)
        name = row[0].split("AED")[0].strip() if row else ""
        if name:
            cars.append({
                "name": name,
                "price_display": m.group(0) if m else "",
                "url": row[-1] if row and row[-1].startswith("http") else "",
            })
    return cars


def _extract_overview_text(soup: BeautifulSoup, heading_pattern: str) -> str:
    """Grabs the intro paragraph(s) directly under a heading (e.g. the
    "<Model> Price In UAE" / "<Model> Specification" hero/intro paragraph),
    stopping at the next heading, table, or list.

    FIX (2026-08-12): this used to only follow a strict chain of sibling <p>
    tags (find_next(["p","h1","h2","h3","table","ul"]) and bail the instant
    the next matched node wasn't a <p>). In practice this template wraps the
    "Read More / Read Less" truncated intro text in a <div> (with the actual
    text living in nested <span>/<div> "read-more" containers), so the very
    first hop after the heading was never a bare <p> and the whole thing came
    back empty on every model — this is the "Hero info" that was reported
    missing. We now walk ALL text-bearing descendants between the heading and
    the next heading/table/list, accepting <p> and <div> blocks (as long as
    they carry direct/substantial text and aren't just wrapping the "Read
    More Read Less" toggle controls), which is robust to whichever wrapper
    tag the CMS happens to use.
    """
    heading = _find_heading(soup, heading_pattern)
    if heading is None:
        return ""

    stop_tags = {"h1", "h2", "h3", "table", "ul"}
    skip_texts = {"read more", "read less", "read more read less"}

    parts: list[str] = []
    seen: set[str] = set()
    node = heading
    hops = 0
    while hops < 60:
        node = node.find_next(["p", "div", "span", "h1", "h2", "h3", "table", "ul"])
        hops += 1
        if node is None or node.name in stop_tags:
            break
        # Skip container tags that themselves hold block-level children —
        # we only want to capture text at the "leaf" paragraph level so we
        # don't duplicate the same sentence once per wrapper div.
        if node.find(["p", "div", "h1", "h2", "h3", "table", "ul"]):
            continue
        txt = _clean_ws(node.get_text(" ", strip=True))
        if not txt or txt.lower() in skip_texts or len(txt) < 25:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        parts.append(txt)
        # The intro is normally 1-3 paragraphs; once we've collected a few
        # substantial chunks, a 4th text-bearing node is almost always the
        # start of the next section (e.g. a variant card) rather than more
        # intro copy, so stop there.
        if len(parts) >= 4:
            break
    return " ".join(parts)


def _extract_updates(soup: BeautifulSoup) -> list[dict]:
    """The '<Model> <Year> Updates' timeline (dated bullet list)."""
    heading = _find_heading(soup, r"Updates$")
    if heading is None:
        return []
    updates = []
    node = heading
    for _ in range(60):
        node = node.find_next(["li", "p", "h1", "h2", "h3"])
        if node is None or node.name in ("h1", "h2", "h3"):
            break
        txt = _clean_ws(node.get_text(" ", strip=True))
        m = re.match(r"([A-Za-z]+ \d{1,2},?\s*\d{4})\s*:?\s*(.+)", txt)
        if m:
            updates.append({"date": m.group(1), "text": m.group(2)})
        elif txt and len(txt) > 20:
            updates.append({"date": "", "text": txt})
        if len(updates) >= 12:
            break
    return updates


def parse_model_page(url: str, images_dir: Path | None = None) -> dict:
    """Scrape one zigwheels.ae car model page (+ /specifications, /colors)
    and return a structured dict."""
    log.info("  Scraping: %s", url)
    soup, html = fetch(url)
    if soup is None:
        return {"url": url, "error": "fetch_failed", "variants": []}

    brand_slug, model_slug = _brand_model_from_url(url)
    brand = brand_slug.replace("-", " ").title()
    model = model_slug.replace("-", " ").title()

    title_tag = soup.find("h1")
    post_title = _clean_ws(title_tag.get_text(" ", strip=True)) if title_tag else f"{brand} {model}"

    result: dict = {
        "url": url,
        "brand": brand,
        "model": model,
        "post_title": post_title,
        "model_year": "",
        "rating": "",
        "review_count": "",
        "gallery_images": {"exterior": [], "interior": [], "color": []},
        "gallery_local_paths": [],
        "colours": [],
        "key_specs": {},
        "specifications": [],
        "features": [],
        "body_type": "",
        "variants": [],
        "faqs": [],
        "similar_cars": [],
        "updates": [],
        "price_overview_text": "",
        "spec_overview_text": "",
        # Narrative text for the What's New section (Summary/Interior/
        # Exterior/Engine & Fuel Consumption tabs of the Review accordion).
        # NOTE: "updates" above is the "<Model> <Year> Updates" dated
        # timeline — that goes to *Latest Updates*, not What's New. See
        # acf_mapper._build_whats_new_acf / _build_latest_updates_acf.
        "review_narrative": {"introduction": [], "exterior": [], "interior": [], "powertrain": []},
    }

    m_year = re.search(r"\b(20[2-3]\d)\b", post_title)
    if m_year:
        result["model_year"] = m_year.group(1)

    result.update(_extract_price(soup, html))
    rating, review_count = _extract_rating(soup)
    result["rating"] = rating
    result["review_count"] = review_count
    result["key_specs"] = _extract_key_specs(soup)
    result["variants"] = _extract_variants(soup)
    result["gallery_images"] = _extract_gallery_images(html)
    result["colours"] = _extract_colours(soup, brand, model)
    result["price_overview_text"] = _extract_overview_text(soup, rf"{re.escape(model)} Price In UAE")
    if not result["price_overview_text"]:
        # Last-resort fallback so the "hero"/post_content text is never
        # totally empty even if a particular model's page doesn't render
        # the intro paragraph the way we expect (layout drift, A/B test,
        # etc.) — the <meta name="description"> is reliably present and is
        # itself a short, human-written summary of the car.
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            result["price_overview_text"] = _clean_ws(meta_desc["content"])
    result["similar_cars"] = _extract_similar_cars(soup)
    result["updates"] = _extract_updates(soup)

    # ---- Real categorized specs + true variant-wise spec/feature data -----
    # (lives in the review accordion + variant-comparison table + variant
    # feature cards on THIS page — the /specifications sub-page below only
    # server-renders its intro paragraph, see _extract_categorized_specs()).
    result["specifications"] = _extract_categorized_specs(soup)
    result["review_narrative"] = _extract_review_narrative(soup)

    variant_spec_matrix = _extract_variant_spec_table(soup)
    variant_spec_norm = {_norm_variant_key(k, model): v for k, v in variant_spec_matrix.items()}

    raw_feature_lists = _extract_variant_feature_lists(soup)
    feature_lists = _merge_variant_features(raw_feature_lists)
    feature_lists_norm = {_norm_variant_key(k, model): v for k, v in feature_lists.items()}

    for v in result["variants"]:
        vnorm = _norm_variant_key(v["variant_name"], model)
        v["specifications"] = _match_variant_bucket(vnorm, variant_spec_norm)
        v["features"] = [{"name": f, "value": "Yes"} for f in _match_variant_bucket(vnorm, feature_lists_norm)]
    # NOTE: v["specifications"] set here is the OLD/weaker source (the small
    # variant-comparison table on the overview page — a handful of rows like
    # "Wheels"/"Boot Space"). It gets overwritten below, once available, by
    # the real per-variant categorized specs pulled straight from each
    # variant's own tab on the JS-rendered /specifications page (see
    # js_variant_specs a few lines down) — that's the fix for hybrid/EV
    # variants (Battery Capacity, Electric Motor Power, Miscellaneous, etc.)
    # silently sharing whichever variant happened to be the page's default.

    # FIX (2026-08-13): model-level result["features"] was initialised to []
    # above and never actually populated — only the per-variant v["features"]
    # got filled from the "Compare Variants Of <Model>" section. Aggregate
    # the union of all variants' features (deduplicated by name) into the
    # model-level list too, since acf_mapper / wp_uploader read from here
    # for the top-of-page Features summary.
    if not result["features"]:
        seen_feature_names: set[str] = set()
        merged_features: list[dict] = []
        for v in result["variants"]:
            for f in v.get("features") or []:
                key = f["name"].strip().lower()
                if key and key not in seen_feature_names:
                    seen_feature_names.add(key)
                    merged_features.append(f)
        result["features"] = merged_features

    if not result["specifications"] and not variant_spec_matrix and not raw_feature_lists:
        log.warning("  No specifications/variant-feature data found — page layout may have changed "
                    "(check _extract_categorized_specs / _extract_variant_spec_table / _extract_variant_feature_lists)")

    # Recompute price range from variants if the top-of-page price block was missed
    if not result.get("price_min_aed") and result["variants"]:
        prices = [int(v["price_aed"]) for v in result["variants"] if v.get("price_aed", "").isdigit()]
        if prices:
            result["price_min_aed"] = str(min(prices))
            result["price_max_aed"] = str(max(prices))
            result["price_display"] = f"AED {min(prices):,} - {max(prices):,}" if min(prices) != max(prices) else f"AED {min(prices):,}"

    # ---- /specifications sub-page: only its intro paragraph is server-rendered
    spec_url = url.rstrip("/") + "/specifications"
    spec_soup, _spec_html = fetch(spec_url)
    if spec_soup:
        result["spec_overview_text"] = _extract_overview_text(spec_soup, rf"{re.escape(model)} Specification")

    # ---- FIX (2026-08-17): JS-rendered /specifications page (headless
    # browser) — the real source of truth for the full 12-category spec
    # breakdown, scraped PER VARIANT TAB (not just whichever tab is active
    # by default — see _extract_specifications_page_js for why that used to
    # silently drop hybrid/EV-only fields). Tried FIRST; the Review-accordion
    # categories captured earlier (result["specifications"], set above) are
    # then merged UNDERNEATH this — i.e. JS-rendered data wins on any name
    # collision, accordion data only fills genuine gaps the JS page didn't
    # have.
    js_specs, js_variant_specs = _extract_specifications_page_js(url)
    if js_specs:
        existing_keys = {(_clean_ws(s["category"]).lower(), _clean_ws(s["name"]).lower())
                          for s in js_specs}
        merged = list(js_specs)
        for s in result.get("specifications", []):
            key = (_clean_ws(s.get("category", "")).lower(), _clean_ws(s.get("name", "")).lower())
            if key not in existing_keys:
                merged.append(s)
                existing_keys.add(key)
        result["specifications"] = merged
        log.info(f"  Merged {len(js_specs)} JS-rendered spec rows "
                  f"({len(merged)} total after combining with accordion data)")

    # ---- Wire each variant's REAL, independently-scraped spec set --------
    # (this is the actual fix for "hybrid battery capacity / Miscellaneous
    # fields not coming" — previously every variant fell back to whichever
    # tab the /specifications page defaulted to, or to the generic
    # FAQ-mined specs below, so a Hybrid/EV variant's own battery/motor
    # figures and any variant-specific Miscellaneous rows never appeared.)
    if js_variant_specs:
        js_variant_norm = {_norm_variant_key(name, model): specs
                            for name, specs in js_variant_specs.items()}
        for v in result["variants"]:
            vnorm = _norm_variant_key(v["variant_name"], model)
            real_specs = _match_variant_bucket(vnorm, js_variant_norm)
            if real_specs:
                v["specifications"] = real_specs
            elif not v.get("specifications"):
                # No per-variant tab matched this variant name (rare — e.g.
                # a naming mismatch) — better to show the generic model-level
                # JS specs than nothing at all.
                v["specifications"] = list(js_specs)

    # ---- /colors sub-page (fallback if colours weren't found on the overview page)
    if not result["colours"]:
        colors_url = url.rstrip("/") + "/colors"
        colors_soup, _ = fetch(colors_url)
        if colors_soup:
            result["colours"] = _extract_colours(colors_soup, brand, model)

    # ---- /faq sub-page (overview page only shows a handful; the sub-page has more)
    faq_url = url.rstrip("/") + "/faq"
    faq_soup, _ = fetch(faq_url)
    if faq_soup:
        faqs = _extract_faqs(faq_soup)
        if faqs:
            result["faqs"] = faqs
    if not result["faqs"]:
        result["faqs"] = _extract_faqs(soup)

    # ---- FAQ-mined spec supplement -----------------------------------------
    # FIX (2026-08-13) — Bug 12: this used to be a pure fallback ("only fills
    # the gap when result['specifications'] is completely empty"), so on
    # models that DO have a Review accordion, real figures that only appear
    # in FAQ prose (boot/cargo capacity, tyre size, alloy wheel size, etc.
    # — e.g. Elantra's "Cargo volume of 474 L" FAQ, which never showed up
    # anywhere else on the page) were permanently lost. Now FAQ-mined specs
    # are always merged in underneath the accordion data (accordion values
    # win on any name collision, FAQ only fills genuine gaps).
    faq_specs = _extract_specs_from_faqs(result["faqs"])
    if faq_specs:
        have = {(_normalise_category(s.get("category", "")), _normalise_faq_spec(s.get("name", "")))
                for s in result["specifications"]}
        added = 0
        for s in faq_specs:
            key = (_normalise_category(s.get("category", "")), _normalise_faq_spec(s.get("name", "")))
            if key not in have:
                result["specifications"].append(s)
                have.add(key)
                added += 1
        if added:
            log.info("  Recovered %d additional spec(s) from FAQ text not present in the accordion", added)
        for v in result["variants"]:
            if not v.get("specifications"):
                v["specifications"] = faq_specs

    # ---- Body type / fuel type (Bug 11) ------------------------------------
    result["body_type"] = _infer_body_type(result)
    if not result["key_specs"].get("Fuel Type"):
        inferred_fuel = _infer_fuel_type(result)
        if inferred_fuel:
            result["key_specs"]["Fuel Type"] = inferred_fuel

    # ---- Flatten gallery buckets into one capped list for image download ----
    all_images = (
        result["gallery_images"].get("exterior", [])
        + result["gallery_images"].get("interior", [])
        + result["gallery_images"].get("color", [])
    )
    if MAX_IMAGES and len(all_images) > MAX_IMAGES:
        all_images = all_images[:MAX_IMAGES]

    if all_images and images_dir is not None:
        model_img_dir = images_dir / model_slug
        log.info("  Converting %d gallery images to WebP → %s", len(all_images), model_img_dir)
        local_paths = _download_and_convert_images(all_images, model_img_dir, model_slug)
        result["gallery_local_paths"] = local_paths
        log.info("  WebP images saved: %d", len(local_paths))

    return result


def _download_and_convert_images(
    image_urls: list[str],
    save_dir: Path,
    model_slug: str,
    max_width: int = 1200,
    quality: int = 82,
) -> list[str]:
    """
    Download each image URL, convert to WebP (max_width × auto height),
    save to save_dir, and return a list of local file paths.

    Requirements: pip install Pillow requests
    Falls back gracefully if Pillow is not installed (returns original URLs).
    """
    if not image_urls:
        return []

    try:
        from PIL import Image
        import io
    except ImportError:
        log.warning(
            "Pillow not installed — skipping WebP conversion. "
            "Run: pip install Pillow"
        )
        return list(image_urls)

    save_dir.mkdir(parents=True, exist_ok=True)

    dl_session = requests.Session()
    dl_session.headers.update({
        "User-Agent": BASE_HEADERS["User-Agent"],
        "Referer": f"{ZIGWHEELS_BASE}/",
    })

    local_paths = []
    for idx, url in enumerate(image_urls, 1):
        raw_name = urlparse(url).path.split("/")[-1].split("?")[0]
        stem = re.sub(r"\.(jpe?g|png|webp|gif|avif)$", "", raw_name, flags=re.I)
        stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem)
        if not stem:
            stem = f"{model_slug}_{idx}"
        filename = f"{stem}.webp"
        out_path = save_dir / filename

        if out_path.exists():
            log.debug("  Image already exists: %s", out_path)
            local_paths.append(str(out_path))
            continue

        log.info("  [%d/%d] Downloading: %s", idx, len(image_urls), url[-70:])
        try:
            resp = dl_session.get(url, timeout=30, stream=True)
            if resp.status_code != 200:
                log.warning("    Download HTTP %s — skipping", resp.status_code)
                continue
            raw_bytes = resp.content
        except Exception as exc:
            log.warning("Download error: %s", exc)
            continue

        if REMOVE_LOGO:
            try:
                log.info("    Removing source logo/watermark…")
                raw_bytes = remove_logo_from_bytes(raw_bytes)
            except Exception as exc:
                log.warning("    Logo removal failed, keeping original image: %s", exc)
            finally:
                if idx < len(image_urls) and LOGO_REMOVAL_DELAY > 0:
                    log.info("    Waiting %.0fs before next Vertex AI call…", LOGO_REMOVAL_DELAY)
                    time.sleep(LOGO_REMOVAL_DELAY)

        try:
            img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            if img.width > max_width:
                new_height = int(img.height * max_width / img.width)
                img = img.resize((max_width, new_height), Image.LANCZOS)
            img.save(out_path, "WEBP", quality=quality, method=6)
            log.info(
                "Saved WebP: %s  (%dx%d, %.1fKB)",
                filename, img.width, img.height, out_path.stat().st_size / 1024,
            )
            local_paths.append(str(out_path))
        except Exception as exc:
            log.warning("    Image conversion error: %s", exc)
        time.sleep(0.1)
    return local_paths


# ============================================================================
# CSV / JSON output
# ============================================================================

VARIANT_CSV_FIELDS = [
    "brand", "model", "model_year", "car_url",
    "price_min_aed", "price_max_aed", "price_display", "price_city",
    "monthly_installment_aed", "rating", "review_count",
    "variant_name", "variant_url", "variant_price_aed", "variant_price_display",
    "variant_transmission", "variant_engine_cc", "variant_power", "variant_spec_summary",
    "colours_json", "gallery_images_json", "gallery_local_paths_json",
    "key_specs_json", "specifications_json",
]

SPEC_CSV_FIELDS = [
    "brand", "model", "variant_name", "record_type", "category", "name", "value",
]


def models_to_variant_rows(all_models: list) -> tuple[list, list]:
    """One CSV row per variant with JSON columns for nested data."""
    rows = []
    for model in all_models:
        if "error" in model:
            continue
        colours_json = json.dumps(model.get("colours", []), ensure_ascii=False)
        gallery_json = json.dumps(model.get("gallery_images", {}), ensure_ascii=False)
        gallery_local_json = json.dumps(model.get("gallery_local_paths", []), ensure_ascii=False)
        key_specs_json = json.dumps(model.get("key_specs", {}), ensure_ascii=False)
        specs_json = json.dumps(model.get("specifications", []), ensure_ascii=False)

        base = {
            "brand": model.get("brand", ""),
            "model": model.get("model", ""),
            "model_year": model.get("model_year", ""),
            "car_url": model.get("url", ""),
            "price_min_aed": model.get("price_min_aed", ""),
            "price_max_aed": model.get("price_max_aed", ""),
            "price_display": model.get("price_display", ""),
            "price_city": model.get("price_city", ""),
            "monthly_installment_aed": model.get("monthly_installment_aed", ""),
            "rating": model.get("rating", ""),
            "review_count": model.get("review_count", ""),
            "colours_json": colours_json,
            "gallery_images_json": gallery_json,
            "gallery_local_paths_json": gallery_local_json,
            "key_specs_json": key_specs_json,
            "specifications_json": specs_json,
        }

        variants = model.get("variants", [])
        if not variants:
            row = dict(base)
            for k in ("variant_name", "variant_url", "variant_price_aed", "variant_price_display",
                      "variant_transmission", "variant_engine_cc", "variant_power", "variant_spec_summary"):
                row[k] = ""
            rows.append(row)
            continue

        for v in variants:
            row = dict(base)
            row.update({
                "variant_name": v.get("variant_name", ""),
                "variant_url": v.get("variant_url", ""),
                "variant_price_aed": v.get("price_aed", ""),
                "variant_price_display": v.get("price_display", ""),
                "variant_transmission": v.get("transmission", ""),
                "variant_engine_cc": v.get("engine_cc", ""),
                "variant_power": v.get("power", ""),
                "variant_spec_summary": v.get("spec_summary", ""),
            })
            rows.append(row)

    return rows, VARIANT_CSV_FIELDS


def models_to_spec_rows(all_models: list) -> tuple[list, list]:
    """Long-format specifications (easy relational DB import)."""
    rows = []
    for model in all_models:
        if "error" in model:
            continue
        brand = model.get("brand", "")
        model_name = model.get("model", "")
        for name, value in (model.get("key_specs") or {}).items():
            rows.append({
                "brand": brand, "model": model_name, "variant_name": "",
                "record_type": "key_spec", "category": "Key Specs", "name": name, "value": value,
            })
        for item in model.get("specifications", []):
            if not isinstance(item, dict):
                continue
            rows.append({
                "brand": brand, "model": model_name, "variant_name": "",
                "record_type": "specification",
                "category": item.get("category", ""),
                "name": item.get("name", ""),
                "value": item.get("value", ""),
            })
    return rows, SPEC_CSV_FIELDS


def is_under_budget(model_data: dict, max_aed: float = 0) -> bool:
    """max_aed <= 0 means no filtering (UAE listings range far wider than a
    fixed cap makes sense for by default)."""
    if not max_aed:
        return True
    price_val = model_data.get("price_min_aed", "")
    try:
        return float(price_val) <= max_aed
    except (TypeError, ValueError):
        return True


def save_csv(rows: list, fieldnames: list, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("CSV saved  → %s  (%d rows, %d columns)", path, len(rows), len(fieldnames))


def save_json(data: list, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("JSON saved → %s  (%d models)", path, len(data))


def main():
    parser = argparse.ArgumentParser(
        description="ZigWheels UAE (zigwheels.ae) scraper — sitemap or single URL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sitemap", help="Sitemap XML index URL (e.g. https://www.zigwheels.ae/sitemap.xml)")
    group.add_argument("--url",     nargs="+", help="One or more car model URLs (space-separated)")
    group.add_argument("--urls-file", help="Path to a text file with one car model URL per line")

    parser.add_argument("--max",       type=int,   default=0,  help="Max models to scrape (0=all)")
    parser.add_argument("--max-price", type=float, default=0,  help="Skip cars whose min price exceeds this many AED (0=no filter)")
    parser.add_argument("--output",   default="",               help="Output filename stem (no extension)")
    parser.add_argument("--delay",    type=float, default=2.0,  help="Seconds between page requests (default 2)")
    parser.add_argument("--selenium", action="store_true",      help="Use Selenium Chrome (needed if requests get 403)")
    parser.add_argument("--outdir",   default=".",              help="Output directory (default: current dir)")
    parser.add_argument("--images-dir", default="",            help="Directory to save downloaded WebP images (default: <outdir>/images)")
    parser.add_argument("--skip-images", action="store_true",  help="Skip image download/conversion")
    parser.add_argument("--max-images", type=int, default=5,
                        help="Max gallery images to scrape/download per model (default: 5, 0=unlimited)")
    parser.add_argument("--remove-logo", action="store_true",
                        help="Remove source-site logo/watermark from gallery images via "
                             "Vertex AI before saving (needs token.json/client_secret.json/mask.png)")
    parser.add_argument("--logo-delay", type=float, default=15.0,
                        help="Seconds to wait between Vertex AI logo-removal calls (default 15.0)")
    parser.add_argument("--rephrase-content", action="store_true",
                        help="Rephrase post_content + whats-new lists in the ACF JSON "
                             "via Gemini before saving (needs GEMINI_API_KEY in .env)")
    parser.add_argument("--rephrase-delay", type=float, default=1.0,
                        help="Seconds to sleep between rephrase API calls (default 1.0)")
    args = parser.parse_args()

    global USE_SELENIUM, REMOVE_LOGO, MAX_IMAGES, LOGO_REMOVAL_DELAY
    USE_SELENIUM = args.selenium
    REMOVE_LOGO = args.remove_logo
    MAX_IMAGES = args.max_images
    LOGO_REMOVAL_DELAY = args.logo_delay

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = args.output or f"zigwheels_{ts}"
    out_dir = Path(args.outdir)
    out_csv       = out_dir / f"{stem}.csv"
    out_specs_csv = out_dir / f"{stem}_specifications.csv"
    out_json      = out_dir / f"{stem}.json"
    out_acf_json  = out_dir / f"{stem}_acf.json"

    images_dir: Path | None = None
    if not args.skip_images:
        images_dir = Path(args.images_dir) if args.images_dir else out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        log.info("Images dir       : %s", images_dir)

    try:
        import importlib, sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        _acf_mapper = importlib.import_module("acf_mapper")
        _map_all = _acf_mapper.map_all_models
        log.info("ACF mapper     : loaded (will generate _acf.json)")
    except ImportError:
        _map_all = None
        log.warning("acf_mapper.py not found alongside scrapper.py — skipping _acf.json output")

    _rephrase_fn = None
    if args.rephrase_content:
        try:
            _rephrase_fn = lambda models: rephrase_models(models, delay_between=args.rephrase_delay)
            log.info("Content rephraser : ON (Gemini, GEMINI_API_KEY from .env)")
        except Exception as exc:
            log.warning("Content rephraser failed to initialise: %s", exc)

    if args.sitemap:
        urls = parse_sitemap(args.sitemap)
    elif args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    else:
        urls = args.url

    if not urls:
        log.error("No URLs to scrape. Exiting.")
        sys.exit(1)

    if args.max and args.max > 0:
        log.info("Limiting to first %d models (of %d found)", args.max, len(urls))
        urls = urls[:args.max]

    log.info("=" * 60)
    log.info("Models to scrape : %d", len(urls))
    log.info("Output CSV       : %s", out_csv)
    log.info("Output specs CSV : %s", out_specs_csv)
    log.info("Output JSON      : %s", out_json)
    log.info("Selenium mode    : %s", USE_SELENIUM)
    log.info("Delay (seconds)  : %.1f", args.delay)
    log.info("Max price filter : %s", f"≤ AED {args.max_price:,.0f}" if args.max_price else "none")
    log.info("Images           : %s", "SKIP" if args.skip_images else str(images_dir))
    log.info("Max images/model : %s", "unlimited" if MAX_IMAGES == 0 else MAX_IMAGES)
    log.info("Logo removal     : %s", f"ON (Vertex AI, {LOGO_REMOVAL_DELAY:.0f}s between calls)" if REMOVE_LOGO else "OFF")
    log.info("Content rephrase : %s", "ON (Gemini)" if _rephrase_fn else "OFF")
    log.info("=" * 60)

    all_models = []
    skipped    = 0
    for i, url in enumerate(urls, 1):
        log.info("[%d / %d]", i, len(urls))
        model_data = parse_model_page(url, images_dir=images_dir)

        if "error" not in model_data and not is_under_budget(model_data, max_aed=args.max_price):
            log.info("  SKIPPED (price AED %s > %s) — %s %s",
                     model_data.get("price_min_aed", "?"), args.max_price,
                     model_data.get("brand", ""), model_data.get("model", ""))
            skipped += 1
        else:
            all_models.append(model_data)

        if i % 10 == 0:
            rows, fields = models_to_variant_rows(all_models)
            save_csv(rows, fields, out_csv)
            spec_rows, spec_fields = models_to_spec_rows(all_models)
            save_csv(spec_rows, spec_fields, out_specs_csv)
            save_json(all_models, out_json)
            if _map_all:
                acf_data = _map_all(all_models)
                if _rephrase_fn:
                    acf_data = _rephrase_fn(acf_data)
                save_json(acf_data, out_acf_json)

        if i < len(urls):
            time.sleep(args.delay + random.uniform(0.5, 1.5))

    rows, fieldnames = models_to_variant_rows(all_models)
    save_csv(rows, fieldnames, out_csv)
    spec_rows, spec_fields = models_to_spec_rows(all_models)
    save_csv(spec_rows, spec_fields, out_specs_csv)
    save_json(all_models, out_json)
    if _map_all:
        acf_data = _map_all(all_models)
        if _rephrase_fn:
            log.info("Rephrasing content for %d models via Gemini…", len(acf_data))
            acf_data = _rephrase_fn(acf_data)
        save_json(acf_data, out_acf_json)
        log.info("ACF JSON saved → %s", out_acf_json)

    errors = [m for m in all_models if "error" in m]
    log.info("=" * 60)
    log.info("Done!  Scraped: %d  |  Skipped (budget filter): %d  |  Rows in CSV: %d  |  Errors: %d",
             len(all_models), skipped, len(rows), len(errors))
    if errors:
        log.warning("Failed URLs:")
        for m in errors:
            log.warning("  %s", m["url"])

    print(f"\nDone. Variants CSV       -> {out_csv.resolve()}")
    print(f"Done. Specifications CSV -> {out_specs_csv.resolve()}")
    print(f"Done. JSON               -> {out_json.resolve()}")
    if _map_all:
        print(f"Done. ACF JSON (WordPress) -> {out_acf_json.resolve()}")
    if images_dir:
        print(f"Done. WebP Images        -> {images_dir.resolve()}")


if __name__ == "__main__":
    main()