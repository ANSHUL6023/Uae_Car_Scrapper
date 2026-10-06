#!/usr/bin/env python3
"""
wp_uploader.py  —  Upload / sync ACF car data to WordPress
Reads the ACF-mapped JSON produced by acf_mapper.py and pushes each car
to WordPress using the REST API.

WHAT THIS DOES
  1. Resolves / creates the Brand Name taxonomy term (brand-name).
  2. Resolves / creates the Car Category taxonomy term (car-category).
  3. Creates or updates the `cars-data` post with:
       - Cars Overview ACF fields (launched_date, price_range, body_type, etc.)
       - Key Specifications ACF fields (engine_cc, no_of_airbags, etc.)
       - Gallery uploaded from LOCAL WebP files (pre-converted by scrapper.py)
  4. Creates or updates one `car-variant` post per trim with:
       - Specifications repeaters (engine_and_performance, dimensions_and_weight, etc.)
       - Features repeaters (comfort_and_convenience, safety_and_security, etc.)

FIX LOG (2026-06-15):
  Bug 1 — ACF field groups had show_in_rest disabled in WordPress, so REST writes
           were silently ignored (posts created, acf always empty).
           Fix: import acf-export-2026-06-15-rest-enabled.json and re-run upload.

  Bug 2 — ACF REST API expects field *names* (max_torque, comfort_and_convenience),
           not field keys (field_6a…). Repeater rows must use spec_name/spec_value.
           Fixed: acf_mapper now outputs name-based payloads; uploader converts
           legacy key-based JSON automatically.

  Bug 3 — Variant basic fields (variant_name, price_display, etc.) do not exist as
           separate ACF plain-text fields on car-variant in the ACF export. They are
           stored as standard WP post fields (title, excerpt, meta).

USAGE
-----
  python wp_uploader.py cars_acf.json \\
      --wp-url https://yoursite.com \\
      --user admin \\
      --password "xxxx xxxx xxxx xxxx" \\
      --images-dir ./images

  # Dry run (no writes)
  python wp_uploader.py cars_acf.json --dry-run --max 1 ...

  # Skip variant uploads
  python wp_uploader.py cars_acf.json --skip-variants ...

  # Skip image uploads
  python wp_uploader.py cars_acf.json --skip-images ...

Requirements
------------
  pip install requests
"""

import argparse
import base64
import json
import logging
import mimetypes
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


def _configure_stdio() -> None:
    """Use UTF-8 on Windows so status symbols (e.g. checkmarks) don't crash output."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Taxonomy REST slugs
# ---------------------------------------------------------------------------
TAXONOMY_BRAND    = "brand-name"
TAXONOMY_CATEGORY = "car-category"

# ---------------------------------------------------------------------------
# ACF field keys — used to build the flat "acf": {field_key: value} payloads.
# These must exactly match the field keys in the ACF export.
# ---------------------------------------------------------------------------

# Cars Overview (cars-data post) — group_6a2952b35c81d
MODEL_OVERVIEW_FIELDS = {
    "cars_gallery":  "field_6a2f7d87e8034",
    "launched_date": "field_6a295294cc3d4",
    "price_range":   "field_6a295419cc3d5",
    "body_type":     "field_6a29543ccc3d6",
    "fuel_type":     "field_6a29544bcc3d7",
    "transmission":  "field_6a29545bcc3d8",
    "mileage":       "field_6a295465cc3d9",
}

# Key Specifications (cars-data post) — group_6a29555155e67
KEY_SPEC_FIELDS = {
    "engine_cc":          "field_6a295532bb528",
    "max_torque":         "field_6a295552bb529",
    "no_of_airbags":      "field_6a295561bb52a",
    "bootspace":          "field_6a295566bb52b",
    "ground_clearance":   "field_6a295571bb52c",
    "fuel_tank_capacity": "field_6a29557bbb52d",
    "seating_capacity":   "field_6a29558bbb52e",
    "drive_type":         "field_6a295594bb52f",
}

# Parent Base Model (car-variant post) — group_6a2fe6f6e75cf
PARENT_BASE_MODEL_FIELD = "parent_base_model"

# What's New Content (cars-data post) — group_6a29578691443
WHATS_NEW_ACF_FIELDS = {
    "introduction_list": "field_6a2957676bf68",
    "introduction_item": "field_6a2957a06bf69",
    "exterior_list":     "field_6a2958996bf6b",
    "exterior_item":     "field_6a2958a96bf6c",
    "interior_list":     "field_6a2a392ab6460",
    "interior_item":     "field_6a2a393eb6461",
    "powertrain_list":   "field_6a2a3978b6463",
    "powertrain_item":   "field_6a2a3985b6464",
}

WHATS_NEW_REPEATER_NAMES = {
    "introduction_list": "introduction_item",
    "exterior_list":     "exterior_item",
    "interior_list":     "interior_item",
    "powertrain_list":   "powertrain_item",
}

DEFAULT_POST_STATUS = "publish"

# All Specification repeater keys (car-variant post) — group_6a2a3bd25d238
SPEC_REPEATER_KEYS = {
    "engine_and_performance":        "field_6a2a3bcd9b778",
    "dimensions_and_weight":         "field_6a2a4e60ae7e7",
    "capacity":                      "field_6a2a4e63ae7ea",
    "fuel_efficiency":               "field_6a2a4e64ae7ed",
    "suspension_brakes_and_steering": "field_6a2a4e6dae7f0",
    "tyres_and_wheels":              "field_6a2a4e71ae7f3",
    "battery_range_and_charging":    "field_6a2bf0f2292c4",
}

# All Feature repeater keys (car-variant post) — group_6a2a3bd4f1ae6
FEATURE_REPEATER_KEYS = {
    "comfort_and_convenience": "field_6a2a3bcf845cf",
    "locks_and_security":      "field_6a2a4fed749fc",
    "seats_and_upholstery":    "field_6a2a4fec749f9",
    "safety_and_security":     "field_6a2a4fea749f6",
    "lights":                  "field_6a2a4fe4749f3",
    "instrumentation":         "field_6a2a4fe3749f0",
    "entertainment":           "field_6a2a4ff6749ff",
    "advanced_features":       "field_6a2aa821472a1",
    "manufacturer_warranty":   "field_6a2aa826472a4",
}

# Combined lookup: group name → repeater field key (legacy key-based JSON)
ALL_REPEATER_KEYS = {**SPEC_REPEATER_KEYS, **FEATURE_REPEATER_KEYS}

# Reverse lookup: field key → field name (for legacy key-based acf_mapper output)
FIELD_KEY_TO_NAME = {v: k for k, v in {**MODEL_OVERVIEW_FIELDS, **KEY_SPEC_FIELDS}.items()}
REPEATER_KEY_TO_NAME = {v: k for k, v in ALL_REPEATER_KEYS.items()}

def _row_uses_field_keys(row: dict) -> bool:
    return any(str(k).startswith("field_") for k in row)

def _normalize_repeater_rows(rows: list) -> list:
    """Convert legacy key-based repeater rows → {spec_name, spec_value}."""
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if _row_uses_field_keys(row):
            spec_name = spec_value = None
            for sub_key, sub_val in row.items():
                if str(sub_key).startswith("field_"):
                    if spec_name is None:
                        spec_name = sub_val
                    else:
                        spec_value = sub_val
            if spec_name is not None:
                normalized.append({
                    "spec_name":  spec_name,
                    "spec_value": spec_value if spec_value is not None else "",
                })
        else:
            normalized.append(row)
    return normalized


def _normalize_whats_new_rows(rows: list, item_field: str) -> list:
    """Normalize What's New repeater rows to {item_field: text}."""
    normalized = []
    item_key = WHATS_NEW_ACF_FIELDS.get(item_field, item_field)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = (
            row.get(item_field)
            or row.get(item_key)
            or next((v for v in row.values() if isinstance(v, str) and v.strip()), "")
        )
        if text:
            normalized.append({item_field: str(text).strip()})
    return normalized


def _build_whats_new_rows(whats: dict) -> dict:
    """Build What's New repeater payloads from scraped whats_new dict."""
    section_map = {
        "introduction": ("introduction_list", "introduction_item"),
        "exterior":     ("exterior_list",     "exterior_item"),
        "interior":     ("interior_list",     "interior_item"),
        "powertrain":   ("powertrain_list",   "powertrain_item"),
    }
    acf: dict = {}
    for section_key, (repeater_name, item_name) in section_map.items():
        rows = [
            {item_name: str(text).strip()}
            for text in (whats.get(section_key) or [])
            if text
        ]
        if rows:
            acf[repeater_name] = rows
    return acf


def _merge_whats_new_acf(raw: dict, car: dict) -> dict:
    """Ensure all What's New repeater groups from acf_mapper are present."""
    merged = dict(raw)
    sources = [
        car.get("acf_whats_new") or {},
        car.get("acf_model_payload") or {},
    ]

    for source in sources:
        if not source:
            continue
        for repeater_name, item_name in WHATS_NEW_REPEATER_NAMES.items():
            rows = source.get(repeater_name)
            if rows:
                merged[repeater_name] = _normalize_whats_new_rows(rows, item_name)

    whats = car.get("whats_new") or {}
    if whats:
        for name, rows in _build_whats_new_rows(whats).items():
            if rows:
                merged[name] = rows

    return merged


def _acf_payload_for_rest(acf: dict) -> dict:
    """
    Ensure the ACF payload uses field names for the REST API.

    New acf_mapper output already uses names. Older JSON used field_6a… keys;
    convert those here so re-uploads work without re-scraping.
    """
    if not acf:
        return {}

    rest: dict = {}
    for key, value in acf.items():
        if str(key).startswith("field_"):
            field_name = FIELD_KEY_TO_NAME.get(key) or REPEATER_KEY_TO_NAME.get(key)
            if not field_name:
                log.debug("  Unknown ACF field key skipped: %s", key)
                continue
            rest[field_name] = _normalize_repeater_rows(value) if isinstance(value, list) else value
        elif isinstance(value, list):
            if key in WHATS_NEW_REPEATER_NAMES:
                rest[key] = _normalize_whats_new_rows(
                    value, WHATS_NEW_REPEATER_NAMES[key]
                )
            else:
                rest[key] = _normalize_repeater_rows(value)
        else:
            rest[key] = value
    return rest


def check_acf_rest_enabled(client: "WPClient", post_type: str) -> tuple[bool, str]:
    """
    Return (enabled, message). ACF field groups must have show_in_rest enabled
    or every acf write is silently ignored by WordPress.
    """
    try:
        r = client.session.get(
            client._url(post_type),
            params={"per_page": 1, "_fields": "id,acf"},
            timeout=20,
        )
        if r.status_code != 200:
            return False, f"Could not query {post_type} (HTTP {r.status_code})"

        posts = r.json()
        if not posts:
            return True, "No posts yet — ACF REST check deferred until first upload"

        acf = posts[0].get("acf")
        if acf is None:
            return False, (
                "ACF plugin may not be active — REST responses lack an 'acf' property"
            )

        if isinstance(acf, dict) and len(acf) > 0:
            return True, f"ACF REST OK — {len(acf)} field(s) visible on {post_type}"

        return False, (
            "ACF field groups have show_in_rest DISABLED — posts save but all custom "
            "fields stay empty. In WP Admin: ACF -> each field group -> Settings -> "
            "enable 'Show in REST API'. Or re-import "
            "acf-export-2026-06-15-rest-enabled.json via ACF -> Tools -> Import."
        )
    except Exception as exc:
        return False, f"ACF REST check failed: {exc}"


def _resolve_local_path(path_str: str) -> str:
    """Resolve relative image paths against the project directory."""
    p = Path(path_str)
    if p.is_absolute() and p.exists():
        return str(p)
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        candidate = base / p
        if candidate.exists():
            return str(candidate)
    return path_str


def _build_acf_model_payload(car: dict, gallery_ids: list[int]) -> dict:
    """
    Build the flat ACF payload for a cars-data post using field names.

    acf_mapper.py produces acf_model_payload as {field_name: value}. Legacy JSON
    may still use field_6a… keys — _acf_payload_for_rest() normalizes those.
    """
    raw = dict(car.get("acf_model_payload") or {})

    if not raw:
        raw = {}
        for field_name, value in (car.get("acf_overview") or {}).items():
            if value not in (None, "", [], {}):
                raw[field_name] = value
        for field_name, value in (car.get("acf_key_specs") or {}).items():
            if value not in (None, "", [], {}):
                raw[field_name] = value

    raw = _merge_whats_new_acf(raw, car)
    acf = _acf_payload_for_rest(raw)

    if gallery_ids:
        acf["cars_gallery"] = gallery_ids

    return acf


def _build_acf_variant_payload(variant: dict) -> dict:
    """
    Build the flat ACF payload for a car-variant post using field names.

    Repeaters are {repeater_name: [{spec_name, spec_value}, …]}.
    Legacy key-based JSON is normalized by _acf_payload_for_rest().
    """
    raw = dict(variant.get("acf_variant_payload") or {})

    if not raw:
        old_repeaters = variant.get("acf_repeaters") or {}
        for group_name, rows in old_repeaters.items():
            if rows:
                raw[group_name] = rows
        if raw:
            log.warning(
                "    Variant '%s': using legacy acf_repeaters — re-run acf_mapper "
                "to regenerate acf_variant_payload.",
                variant.get("variant_name", "?"),
            )

    return _acf_payload_for_rest(raw)

class WPClient:
    """Thin wrapper around WordPress REST API with Basic Auth."""

    def __init__(self, wp_url: str, user: str, password: str, dry_run: bool = False):
        self.base    = wp_url.rstrip("/")
        self.dry_run = dry_run

        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "User-Agent":    "UAE-FyndMyCar-Uploader/1.0",
        })

        # Media uploads must NOT send Content-Type: application/json
        self.media_session = requests.Session()
        self.media_session.headers.update({
            "Authorization": f"Basic {creds}",
            "Accept":        "*/*",
            "User-Agent":    "UAE-FyndMyCar-Uploader/1.0",
        })

        # Separate session for downloading remote images (no WP auth needed)
        self.dl_session = requests.Session()
        self.dl_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            )
        })

        # Caches to avoid redundant API calls within one run
        self._media_cache: dict[str, int] = {}   # source_key → attachment_id
        self._term_cache:  dict[str, int] = {}   # "taxonomy:slug" → term_id

    def _url(self, path: str) -> str:
        return f"{self.base}/wp-json/wp/v2/{path.lstrip('/')}"

    def _term_slug(self, name: str) -> str:
        s = name.lower().strip()
        s = re.sub(r"[^a-z0-9\s-]", "", s)
        s = re.sub(r"[\s_]+", "-", s)
        return re.sub(r"-+", "-", s).strip("-")

    def get_or_create_term(self, taxonomy: str, name: str) -> int | None:
        """
        Find an existing term by name in `taxonomy`, or create it if missing.
        Returns the term ID, or None on failure.
        """
        if not name:
            return None

        slug      = self._term_slug(name)
        cache_key = f"{taxonomy}:{slug}"

        if cache_key in self._term_cache:
            return self._term_cache[cache_key]
        try:
            r = self.session.get(
                self._url(taxonomy),
                params={"slug": slug, "_fields": "id,slug,name"},
                timeout=15,
            )
            if r.status_code == 200:
                terms = r.json()
                if terms:
                    term_id = terms[0]["id"]
                    self._term_cache[cache_key] = term_id
                    log.debug("    Term '%s' found [id=%d] in %s", name, term_id, taxonomy)
                    return term_id
        except Exception as exc:
            log.warning("get_term error (%s): %s", taxonomy, exc)

        if self.dry_run:
            log.info("  [DRY RUN] Would CREATE term '%s' in %s", name, taxonomy)
            return 0

        try:
            r = self.session.post(
                self._url(taxonomy),
                json={"name": name, "slug": slug},
                timeout=15,
            )
            if r.status_code in (200, 201):
                term_id = r.json().get("id")
                self._term_cache[cache_key] = term_id
                log.info("    ✓ Created term '%s' [id=%d] in %s", name, term_id, taxonomy)
                return term_id
            elif r.status_code == 400 and "term_exists" in r.text:
                data = r.json()
                term_id = (data.get("data", {}) or {}).get("term_id") or (
                    data.get("additional_data", {}) or {}
                ).get("term_id")
                if term_id:
                    self._term_cache[cache_key] = term_id
                    return term_id
            log.error("  CREATE term failed HTTP %s: %s", r.status_code, r.text[:300])
        except Exception as exc:
            log.error("  CREATE term exception (%s): %s", taxonomy, exc)

        return None
    
    def find_by_slug(self, post_type: str, slug: str) -> dict | None:
        try:
            r = self.session.get(
                self._url(post_type),
                params={"slug": slug, "_fields": "id,slug,title,status"},
                timeout=20,
            )
            if r.status_code == 200:
                posts = r.json()
                return posts[0] if posts else None
        except Exception as exc:
            log.warning("find_by_slug error: %s", exc)
        return None

    def find_by_title(self, post_type: str, title: str) -> dict | None:
        try:
            r = self.session.get(
                self._url(post_type),
                params={"search": title, "per_page": 5, "_fields": "id,slug,title,status"},
                timeout=20,
            )
            if r.status_code == 200:
                for post in r.json():
                    wp_title = post.get("title", {}).get("rendered", "")
                    if wp_title.strip().lower() == title.strip().lower():
                        return post
        except Exception as exc:
            log.warning("find_by_title error: %s", exc)
        return None

    def create(self, post_type: str, payload: dict) -> dict | None:
        if self.dry_run:
            log.info("  [DRY RUN] Would CREATE: %s", payload.get("title", "?"))
            return {"id": 0, "_dry_run": True}
        try:
            r = self.session.post(self._url(post_type), json=payload, timeout=30)
            if r.status_code in (200, 201):
                return r.json()
            log.error("  CREATE failed HTTP %s: %s", r.status_code, r.text[:400])
        except Exception as exc:
            log.error("  CREATE exception: %s", exc)
        return None

    def update(self, post_type: str, post_id: int, payload: dict) -> dict | None:
        if self.dry_run:
            log.info("  [DRY RUN] Would UPDATE id=%d: %s", post_id, payload.get("title", "?"))
            return {"id": post_id, "_dry_run": True}
        try:
            r = self.session.patch(
                self._url(f"{post_type}/{post_id}"),
                json=payload,
                timeout=30,
            )
            if r.status_code in (200, 201):
                return r.json()
            log.error("  UPDATE failed HTTP %s for id=%d: %s", r.status_code, post_id, r.text[:400])
        except Exception as exc:
            log.error("  UPDATE exception: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Media helpers
    # ------------------------------------------------------------------

    def _find_media_by_source_url(self, source_key: str) -> int | None:
        """Returns existing attachment ID if this image was already uploaded."""
        if source_key in self._media_cache:
            return self._media_cache[source_key]
        try:
            r = self.session.get(
                self._url("media"),
                params={
                    "meta_key":   "_source_url",
                    "meta_value": source_key,
                    "per_page":   1,
                    "_fields":    "id,meta",
                },
                timeout=15,
            )
            if r.status_code == 200:
                results = r.json()
                if results:
                    att = results[0]
                    att_id = att.get("id")
                    meta = att.get("meta", {})
                    stored = meta.get("_source_url", "") if isinstance(meta, dict) else ""
                    if stored and stored == source_key:
                        self._media_cache[source_key] = att_id
                        return att_id
        except Exception as exc:
            log.debug("find_media_by_source_url error: %s", exc)
        return None

    def _post_media(self, filename: str, image_bytes: bytes, mime_type: str) -> requests.Response | None:
        """Upload bytes to WP media library via multipart (ModSecurity-friendly)."""
        try:
            return self.media_session.post(
                self._url("media"),
                files={"file": (filename, image_bytes, mime_type)},
                timeout=60,
            )
        except Exception as exc:
            log.warning("    Image upload exception: %s", exc)
            return None

    def _upload_image_bytes(
        self, image_bytes: bytes, filename: str, mime_type: str,
        source_key: str, title: str = "",
    ) -> int | None:
        """Try upload; fall back to JPEG if WebP is blocked by the host."""
        attempts = [(filename, image_bytes, mime_type)]
        if mime_type == "image/webp":
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                buf = io.BytesIO()
                jpeg_name = Path(filename).stem + ".jpg"
                img.save(buf, "JPEG", quality=88)
                attempts.append((jpeg_name, buf.getvalue(), "image/jpeg"))
            except ImportError:
                pass
            except Exception as exc:
                log.debug("    WebP→JPEG conversion skipped: %s", exc)

        for fname, data, mime in attempts:
            r = self._post_media(fname, data, mime)
            if r is None:
                continue
            if r.status_code in (200, 201):
                att_id = r.json().get("id")
                if att_id:
                    self.session.patch(
                        self._url(f"media/{att_id}"),
                        json={"meta": {"_source_url": source_key}, "alt_text": title},
                        timeout=15,
                    )
                    self._media_cache[source_key] = att_id
                return att_id
            if mime != attempts[-1][2]:
                log.debug("    Upload as %s failed HTTP %s — retrying as JPEG", mime, r.status_code)
            else:
                log.warning("    Image upload HTTP %s: %s", r.status_code, r.text[:200])
        return None

    def _upload_local_image(self, local_path: str, title: str = "") -> int | None:
        """Upload a LOCAL WebP/JPEG file to WP Media Library."""
        if self.dry_run:
            return None

        local_path = _resolve_local_path(local_path)
        path_obj = Path(local_path)
        if not path_obj.exists():
            log.warning("    Local image not found: %s", local_path)
            return None

        try:
            image_bytes = path_obj.read_bytes()
        except Exception as exc:
            log.warning("    Local image read error: %s — %s", exc, local_path)
            return None

        filename  = path_obj.name
        mime_type = mimetypes.guess_type(filename)[0] or "image/webp"
        return self._upload_image_bytes(image_bytes, filename, mime_type, local_path, title)

    def _upload_remote_image(self, image_url: str, title: str = "") -> int | None:
        """Download image from URL and upload to WP Media Library (fallback)."""
        if self.dry_run:
            return None
        try:
            resp = self.dl_session.get(image_url, timeout=30, stream=True)
            if resp.status_code != 200:
                log.warning("    Image download HTTP %s: %s", resp.status_code, image_url)
                return None
            image_bytes = resp.content
        except Exception as exc:
            log.warning("    Image download error: %s — %s", exc, image_url)
            return None

        if not image_bytes:
            return None

        parsed_path = urlparse(image_url).path
        filename    = parsed_path.split("/")[-1].split("?")[0] or "image.jpg"
        if not re.search(r"\.(jpe?g|png|webp|gif|avif)$", filename, re.I):
            filename += ".jpg"

        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = "image/jpeg"

        return self._upload_image_bytes(image_bytes, filename, mime_type, image_url, title)

    def resolve_gallery_ids(
        self,
        local_paths: list,
        remote_urls: list,
        car_title: str = "",
        max_images: int = 5,
    ) -> list[int]:
        """
        Upload images to WP Media Library.
        Prefers local WebP files (pre-converted by scrapper.py).
        Falls back to remote URL download if no local file available.
        """
        ids: list[int] = []

        sources = []
        for lp in (local_paths or [])[:max_images]:
            sources.append(("local", _resolve_local_path(lp)))

        if len(sources) < max_images:
            remaining = max_images - len(sources)
            for ru in (remote_urls or [])[:remaining]:
                sources.append(("remote", ru))

        total = len(sources)
        for i, (kind, src) in enumerate(sources, 1):
            log.info("    Gallery image [%d/%d] (%s): …%s", i, total, kind, src[-60:])

            existing_id = self._find_media_by_source_url(src)
            if existing_id:
                log.info("      → reusing attachment id=%d", existing_id)
                ids.append(existing_id)
            else:
                new_id = (
                    self._upload_local_image(src, title=car_title)
                    if kind == "local"
                    else self._upload_remote_image(src, title=car_title)
                )
                if new_id:
                    log.info("      → uploaded id=%d", new_id)
                    ids.append(new_id)
                else:
                    log.warning("      → FAILED, skipping")

            if i < total:
                time.sleep(0.3)

        return ids


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _build_model_payload(
    car: dict,
    gallery_ids: list[int],
    brand_term_id:    int | None,
    category_term_id: int | None,
    post_status: str = DEFAULT_POST_STATUS,
) -> dict:
    """
    Build the REST API payload for a cars-data model post.

    BUG 1 FIX: ACF fields are now sent flat with field keys, not wrapped in
    group names. We use acf_model_payload from the fixed acf_mapper.py.
    """
    price_min     = car.get("price_min_aed", "")
    price_display = car.get("price_display", "")
    if not price_display and price_min:
        try:
            price_display = f"AED {int(price_min):,}"
        except (ValueError, TypeError):
            price_display = str(price_min)

    # BUG 1 FIX: flat {field_key: value} — no group wrappers
    acf = _build_acf_model_payload(car, gallery_ids)

    # Build excerpt
    parts = []
    if price_display:
        parts.append(f"Price: {price_display}")
    if car.get("body_type"):
        parts.append(f"Type: {car['body_type']}")
    if car.get("fuel_type"):
        parts.append(f"Fuel: {car['fuel_type']}")

    payload: dict = {
        "title":   car.get("post_title", ""),
        "slug":    car.get("slug", ""),
        "status":  post_status,
        "excerpt": " | ".join(parts),
        "acf":     acf,
        "meta": {
            "source_url":          car.get("source_url", ""),
            "zigwheels_model_slug": car.get("slug", ""),
            # These are NOT registered ACF fields — sent as plain post meta so
            # nothing is lost even before you add matching ACF fields. See the
            # "New ACF fields" note for the proper ACF-field version of these.
            "rating":                  car.get("rating", ""),
            "review_count":            car.get("review_count", ""),
            "monthly_installment_aed": car.get("monthly_installment_aed", ""),
            "price_city":              car.get("price_city", ""),
            "colours_json":            json.dumps(car.get("colours", []), ensure_ascii=False),
            "faqs_json":               json.dumps(car.get("faqs", []), ensure_ascii=False),
            "similar_cars_json":       json.dumps(car.get("similar_cars", []), ensure_ascii=False),
        },
    }

    intro = (car.get("post_content") or car.get("summary") or "").strip()
    if intro:
        payload["content"] = intro

    if brand_term_id:
        payload[TAXONOMY_BRAND] = [brand_term_id]

    if category_term_id:
        payload[TAXONOMY_CATEGORY] = [category_term_id]

    return payload


def _build_variant_payload(
    car: dict,
    variant: dict,
    model_post_id: int,
    post_status: str = DEFAULT_POST_STATUS,
) -> dict:
    """
    Build the REST API payload for a car-variant post.

    BUG 2 FIX: Repeaters are sent as flat {repeater_field_key: [rows]} where
    each row is {spec_name_field_key: choice_key, spec_value_field_key: value}.
    No group-name wrappers. No field-name keys. We use acf_variant_payload from
    the fixed acf_mapper.py.

    BUG 4 FIX: Variant info (name, price, fuel_type, etc.) does not have its own
    ACF plain-text fields in the ACF export. It is stored as:
      - Post title  → "Brand Model – Variant Name"
      - Post excerpt → price display
      - Post meta   → variant_id, parent_model_slug, fuel_type, transmission, mileage
    """
    model_title = car.get("post_title", "")
    model_slug  = car.get("slug", "")
    vname       = variant.get("variant_name", "Unknown")

    clean_vname = re.sub(r"[^a-z0-9]+", "-", vname.lower()).strip("-")
    v_slug      = f"{model_slug}-{clean_vname}" if model_slug else clean_vname

    price_aed     = variant.get("price_aed", "")
    price_display = variant.get("price_display", "")
    if not price_display and price_aed:
        try:
            price_display = f"AED {int(price_aed):,}"
        except (ValueError, TypeError):
            price_display = str(price_aed)

    # BUG 2 FIX: flat {repeater_field_key: [rows_with_field_key_sub_keys]}
    acf = _build_acf_variant_payload(variant)

    if model_post_id:
        acf[PARENT_BASE_MODEL_FIELD] = model_post_id

    return {
        "title":   f"{model_title} – {vname}",
        "slug":    v_slug,
        "status":  post_status,
        "excerpt": price_display,
        "acf":     acf,
        "meta": {
            "parent_model_id":       str(model_post_id),
            "parent_model_slug":     model_slug,
            "transmission":          variant.get("transmission", ""),
            "engine_cc":             variant.get("engine_cc", ""),
            "power":                 variant.get("power", ""),
            "variant_price_aed":     str(price_aed),
            "variant_price_display": price_display,
            "spec_summary":          variant.get("spec_summary", ""),
        },
    }


# ---------------------------------------------------------------------------
# Upload orchestration
# ---------------------------------------------------------------------------

def upload_variants(
    client: WPClient,
    car: dict,
    model_post_id: int,
    post_type: str  = "car-variant",
    delay: float    = 0.3,
    post_status: str = DEFAULT_POST_STATUS,
) -> dict:
    """Create or update one car-variant post per trim."""
    variants = car.get("variants", [])
    if not variants:
        log.info("  —  No variants in data")
        return {"created": 0, "updated": 0, "error": 0}

    log.info("  ↳  Uploading %d variant(s)…", len(variants))
    counts = {"created": 0, "updated": 0, "error": 0}

    for i, variant in enumerate(variants, 1):
        vname   = variant.get("variant_name", f"Variant {i}")
        payload = _build_variant_payload(car, variant, model_post_id, post_status=post_status)

        log.info("    [%d/%d] %s", i, len(variants), vname)

        # Diagnostic: log how many repeater rows will be sent
        acf = payload.get("acf", {})
        total_rows = sum(len(v) for v in acf.values() if isinstance(v, list))
        if total_rows == 0:
            log.warning(
                "      ⚠ No ACF repeater rows for this variant — "
                "check scraper returned specs/features and acf_mapper ran correctly"
            )
        else:
            log.info("         ACF repeater rows: %d across %d group(s)", total_rows, len(acf))

        existing = client.find_by_slug(post_type, payload["slug"])
        if existing is None:
            existing = client.find_by_title(post_type, payload["title"])

        if existing:
            result  = client.update(post_type, existing["id"], payload)
            outcome = "updated" if result else "error"
            if result:
                log.info("      ↻ Updated [id=%d]", existing["id"])
        else:
            result  = client.create(post_type, payload)
            outcome = "created" if result else "error"
            if result and not client.dry_run:
                log.info("      + Created [id=%d]", result.get("id", 0))

        counts[outcome] += 1
        if i < len(variants):
            time.sleep(delay)

    log.info(
        "  ✓  Variants done — created:%d  updated:%d  errors:%d",
        counts["created"], counts["updated"], counts["error"],
    )
    return counts

def upload_car(
    client: WPClient,
    car: dict,
    post_type: str          = "cars-data",
    variant_post_type: str  = "car-variant",
    skip_images: bool       = False,
    skip_variants: bool     = False,
    max_gallery_images: int = 5,
    variant_delay: float    = 0.3,
    post_status: str        = DEFAULT_POST_STATUS,
) -> dict:
    """
    Upload one car model + all its variants.
    Returns: {"model": "created|updated|error", "variants": {counts}}
    """
    title     = car.get("post_title", "Unknown")
    slug      = car.get("slug", "")
    brand     = car.get("brand", "")
    body_type = car.get("body_type", "")

    brand_term_id    = None
    category_term_id = None

    if brand:
        log.info("  ⬡  Brand: '%s'", brand)
        brand_term_id = client.get_or_create_term(TAXONOMY_BRAND, brand)

    if body_type:
        log.info("  ⬡  Category: '%s'", body_type)
        category_term_id = client.get_or_create_term(TAXONOMY_CATEGORY, body_type)

    existing = client.find_by_slug(post_type, slug)
    if existing is None and title:
        existing = client.find_by_title(post_type, title)

    gallery_ids: list[int] = []

    # gallery_images in acf_mapper output: local paths if scraper ran with images,
    # or original remote URLs if scraper ran with --skip-images.
    gallery_images = car.get("gallery_images", [])
    local_paths    = [p for p in gallery_images if p and not str(p).startswith("http")]
    remote_urls    = [p for p in gallery_images if p and str(p).startswith("http")]

    has_images = bool(local_paths or remote_urls)

    if has_images and not skip_images:
        log.info(
            "  ↑  Uploading gallery images (local: %d, remote: %d)…",
            len(local_paths), len(remote_urls),
        )
        gallery_ids = client.resolve_gallery_ids(
            local_paths, remote_urls,
            car_title=title, max_images=max_gallery_images,
        )
        log.info("  ✓  Gallery: %d attachment IDs", len(gallery_ids))
    elif has_images and skip_images:
        log.info("  ⊘  Gallery skipped (--skip-images)")
    else:
        log.info("  —  No gallery images in data")

    payload = _build_model_payload(
        car, gallery_ids, brand_term_id, category_term_id, post_status=post_status,
    )

    if gallery_ids:
        payload["featured_media"] = gallery_ids[0]
        log.info("  ★  Featured image: attachment id=%d", gallery_ids[0])

    whats_counts = {
        name: len(rows)
        for name, rows in payload.get("acf", {}).items()
        if name in WHATS_NEW_REPEATER_NAMES and isinstance(rows, list)
    }
    if whats_counts:
        log.info("  What's New rows: %s", whats_counts)

    # Diagnostic: log ACF field count being sent
    acf_count = len([v for v in payload.get("acf", {}).values() if v not in (None, "", [], {})])
    log.info("  ACF fields to write: %d", acf_count)

    if existing:
        model_post_id = existing["id"]
        log.info("  ↻  Updating model [id=%d]  %s", model_post_id, title)
        result        = client.update(post_type, model_post_id, payload)
        model_outcome = "updated" if result else "error"
    else:
        log.info("  +  Creating model  %s", title)
        result = client.create(post_type, payload)
        model_post_id = result.get("id", 0) if (result and not client.dry_run) else 0
        model_outcome = "created" if result else "error"

    variant_counts = {"created": 0, "updated": 0, "error": 0}

    if not skip_variants and model_post_id and not client.dry_run:
        variant_counts = upload_variants(
            client, car, model_post_id,
            post_type=variant_post_type,
            delay=variant_delay,
            post_status=post_status,
        )
    elif skip_variants:
        log.info("  ⊘  Variants skipped (--skip-variants)")
    elif client.dry_run:
        for v in car.get("variants", []):
            log.info("  [DRY RUN] Would CREATE variant: %s – %s", title, v.get("variant_name", "?"))

    return {"model": model_outcome, "variants": variant_counts}


def main():
    parser = argparse.ArgumentParser(
        description="Upload / sync UAE FyndMyCar (zigwheels.ae-sourced) ACF car data to WordPress",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("acf_json",   help="ACF-mapped JSON file from acf_mapper.py")
    parser.add_argument("--wp-url",   required=True, help="WordPress site URL")
    parser.add_argument("--user",     required=True, help="WordPress username")
    parser.add_argument("--password", required=True, help="WordPress Application Password")
    parser.add_argument("--post-type", default="cars-data",
                        help="Model CPT slug (default: cars-data)")
    parser.add_argument("--variant-post-type", default="car-variant",
                        help="Variant CPT slug (default: car-variant)")
    parser.add_argument("--images-dir", default="",
                        help="Directory containing local WebP images from scrapper.py "
                             "(default: auto-detect from gallery_images paths)")
    parser.add_argument("--delay",         type=float, default=0.5,
                        help="Seconds between car uploads (default: 0.5)")
    parser.add_argument("--variant-delay", type=float, default=0.3,
                        help="Seconds between variant uploads (default: 0.3)")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Simulate — no data written to WordPress")
    parser.add_argument("--force",        action="store_true",
                        help="Upload even when ACF REST is disabled (fields will stay empty)")
    parser.add_argument("--skip-images",  action="store_true",
                        help="Skip gallery image uploads")
    parser.add_argument("--skip-variants", action="store_true",
                        help="Upload model posts only, skip variants")
    parser.add_argument("--max-gallery-images", type=int, default=5,
                        help="Max gallery images per car (default: 20)")
    parser.add_argument("--max", type=int, default=0,
                        help="Stop after N cars (0 = all)")
    parser.add_argument("--status", default=DEFAULT_POST_STATUS,
                        choices=["draft", "publish", "pending", "private"],
                        help="WordPress post status for models and variants (default: publish)")
    args = parser.parse_args()

    in_path = Path(args.acf_json)
    if not in_path.exists():
        print(f"ERROR: File not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    with open(in_path, encoding="utf-8") as f:
        cars = json.load(f)

    if isinstance(cars, dict):
        cars = [cars]
    if args.max and args.max > 0:
        cars = cars[:args.max]

    # If --images-dir provided, patch gallery_images in each car to local paths
    if args.images_dir:
        images_root = Path(args.images_dir)
        for car in cars:
            slug = car.get("slug", "")
            model_dir = images_root / slug
            if model_dir.exists():
                webp_files = sorted(model_dir.glob("*.webp"))
                if webp_files:
                    car["gallery_images"] = [str(p) for p in webp_files]
                    log.debug("Patched gallery for %s: %d local files", slug, len(webp_files))

    total_variants = sum(len(c.get("variants", [])) for c in cars)

    print(f"Loading ACF data from : {in_path}")
    print(f"Cars to upload        : {len(cars)}")
    print(f"Total variants        : {total_variants}")
    print(f"WordPress URL         : {args.wp_url}")
    print(f"Model post type       : {args.post_type}")
    print(f"Variant post type     : {args.variant_post_type}")
    print(f"Gallery image upload  : {'DISABLED' if args.skip_images else f'ENABLED (max {args.max_gallery_images})'}")
    print(f"Variant upload        : {'DISABLED' if args.skip_variants else 'ENABLED'}")
    print(f"Images dir            : {args.images_dir or '(auto from data)'}")
    print(f"Post status           : {args.status}")
    print(f"Dry run               : {args.dry_run}")
    print()

    client = WPClient(
        wp_url=args.wp_url,
        user=args.user,
        password=args.password,
        dry_run=args.dry_run,
    )

    # Connection check
    try:
        test = client.session.get(
            f"{args.wp_url.rstrip('/')}/wp-json/wp/v2/{args.post_type}",
            params={"per_page": 1, "_fields": "id"},
            timeout=15,
        )

        if test.status_code == 401:
            print("ERROR: Auth failed (401). Check --user / --password.", file=sys.stderr)
            sys.exit(1)
        elif test.status_code == 404:
            print(f"ERROR: Post type '{args.post_type}' not found (404).", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"✓ WordPress connection OK ({args.wp_url})")
    except requests.RequestException as exc:
        print(f"ERROR: Cannot connect: {exc}", file=sys.stderr)
        sys.exit(1)

    if not args.skip_variants:
        try:
            vt = client.session.get(
                f"{args.wp_url.rstrip('/')}/wp-json/wp/v2/{args.variant_post_type}",
                params={"per_page": 1, "_fields": "id"},
                timeout=15,
            )
            if vt.status_code == 404:
                print(
                    f"WARNING: '{args.variant_post_type}' CPT returned 404 — "
                    f"variants will be skipped. Make sure show_in_rest=true.",
                    file=sys.stderr,
                )
                args.skip_variants = True
            else:
                print(f"✓ Variant post type OK ({args.variant_post_type})")
        except Exception:
            pass

    # ACF REST preflight — field groups must expose fields or writes are ignored
    acf_ok, acf_msg = check_acf_rest_enabled(client, args.post_type)
    if acf_ok:
        print(f"✓ {acf_msg}")
    else:
        print(f"⚠ ACF REST: {acf_msg}", file=sys.stderr)
        if not args.dry_run and not args.force:
            print(
                "\nERROR: ACF fields cannot be saved until show_in_rest is enabled.\n"
                "  1. WP Admin -> ACF -> Tools -> Import -> acf-export-2026-06-15-rest-enabled.json\n"
                "  2. Or manually enable 'Show in REST API' on every field group\n"
                "  3. Re-run this uploader\n"
                "Use --force to upload anyway (posts only, fields will stay empty).\n",
                file=sys.stderr,
            )
            sys.exit(1)

    print()

    counts = dict(
        models_created=0, models_updated=0, models_error=0,
        variants_created=0, variants_updated=0, variants_error=0,
    )

    for i, car in enumerate(cars, 1):
        title      = car.get("post_title", f"Car #{i}")
        n_variants = len(car.get("variants", []))
        log.info("[%d / %d]  %s  (%d variant(s))", i, len(cars), title, n_variants)

        result = upload_car(
            client, car,
            post_type=args.post_type,
            variant_post_type=args.variant_post_type,
            skip_images=args.skip_images,
            skip_variants=args.skip_variants,
            max_gallery_images=args.max_gallery_images,
            variant_delay=args.variant_delay,
            post_status=args.status,
        )

        counts[f"models_{result['model']}"] += 1
        counts["variants_created"] += result["variants"].get("created", 0)
        counts["variants_updated"] += result["variants"].get("updated", 0)
        counts["variants_error"]   += result["variants"].get("error",   0)

        if i < len(cars):
            time.sleep(args.delay)

    print()
    print("=" * 50)
    print("Upload complete!")
    print(f"  Models   created : {counts['models_created']}")
    print(f"  Models   updated : {counts['models_updated']}")
    print(f"  Models   errors  : {counts['models_error']}")
    print(f"  Variants created : {counts['variants_created']}")
    print(f"  Variants updated : {counts['variants_updated']}")
    print(f"  Variants errors  : {counts['variants_error']}")
    print("=" * 50)

    if args.dry_run:
        print("\n[DRY RUN] — No data was written to WordPress.")


if __name__ == "__main__":
    main()