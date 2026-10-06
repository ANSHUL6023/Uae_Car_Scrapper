# FyndMyCar — Car Data Pipeline
~ CarAndBike.com Full Scraper
~ Build by ANSHUL GUPTA with the help of CLAUDE 4.6 SONNET

Scrape car data from carandbike.com, map it to ACF field keys, and push it to a WordPress site as structured posts with variant-level detail.
scrapper.py  →  raw JSON  →  acf_mapper.py  →  ACF JSON  →  wp_uploader.py  →  WordPress

## Overview

The pipeline has four stages:

| Stage | Script | Input | Output |
|-------|--------|-------|--------|
| Scrape | `scrapper.py` | carandbike.com URLs | `carandbike_YYYYMMDD_HHMMSS.json` |
| Map | `acf_mapper.py` | raw JSON | `carandbike_YYYYMMDD_HHMMSS_acf.json` |
| Upload | `wp_uploader.py` | ACF JSON | WordPress posts |
| Diagnose | `diagnose_acf.py` | — | terminal report |

Each car model becomes one `cars-data` post. Each trim/variant becomes one `car-variant` post linked to its parent model via the `parent_car` ACF Relationship field.

---

## Requirements

**Python 3.10+**

```bash
pip install requests beautifulsoup4 lxml selenium webdriver-manager
```

| Package | Used by | Purpose |
|---------|---------|---------|
| `requests` | all scripts | HTTP calls |
| `beautifulsoup4` + `lxml` | scrapper.py | HTML parsing |
| `selenium` | scrapper.py | JavaScript rendering (optional) |
| `webdriver-manager` | scrapper.py | Auto-installs ChromeDriver |

---

## WordPress Setup

Complete this once before running the uploader.

### 1. Required plugins

| Plugin | Where to get | Why |
|--------|-------------|-----|
| **ACF to REST API** | wordpress.org/plugins/acf-to-rest-api | Exposes ACF fields via REST API — **without this, all ACF data is silently dropped** |
| **Custom Post Type UI** | wordpress.org/plugins/custom-post-type-ui | Registers `car-variant` CPT (optional if you use functions.php) |

### 2. Custom Post Types

Both CPTs must have `show_in_rest: true`.

**cars-data** — already registered. Taxonomies: `brand-name`, `car-category`, `car-tag`.

**car-variant** — register via CPT UI:

| Setting | Value |
|---------|-------|
| Post Type Slug | `car-variant` |
| Show in REST API | **True** ← critical |
| REST API base slug | `car-variant` |
| Supports | title, custom-fields |

### 3. ACF field groups

All field groups must have **Show in REST API → Yes** (bottom of field group edit screen).

Field groups assigned to `car-variant`:
- Variant Core Fields ← new group you created
- Advanced Features
- Battery Range and Charging ← EV fields
- Capacity
- Comfort & Convenience
- Dimensions & Weight
- Engine & Performance
- Entertainment
- Exterior Features
- Fuel Efficiency
- Instrumentation
- Lights
- Locks & Security
- Manufacturer Warranty
- Safety & Security
- Seats & Upholstery
- Suspension Brakes & Steering
- Tyres & Wheels

> **Note:** Do not assign the Gallery Images field group to `car-variant` — variants do not have their own gallery.

### 4. Application Password

1. WP Admin → Users → Profile → scroll to **Application Passwords**
2. Enter a name (e.g. `Pipeline`) → click **Add New**
3. Copy the generated password — it is shown **once only**
4. Use it as `--password "xxxx xxxx xxxx xxxx xxxx xxxx"` (spaces are fine)

---

## Quick Start

```bash
# 1. Install dependencies
pip install requests beautifulsoup4 lxml selenium webdriver-manager

# 2. Scrape one car for testing
python scrapper.py --url https://www.carandbike.com/cars/maruti-suzuki/ertiga

# 3. Map scraped data to ACF fields
python acf_mapper.py carandbike_20260608_101806.json

# 4. Diagnose WordPress + ACF setup
python diagnose_acf.py \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx"

# 5. Dry run — no data written
python wp_uploader.py carandbike_20260608_101806_acf.json \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx" \
  --dry-run --max 1

# 6. Upload 1 car to verify fields fill correctly
python wp_uploader.py carandbike_20260608_101806_acf.json \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx" \
  --max 1

# 7. Full upload
python wp_uploader.py carandbike_20260608_101806_acf.json \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx"
```

---

## Scripts

### scrapper.py

Scrapes car model and variant data from carandbike.com. For each model it collects: gallery images, colours, price range, body type, fuel types, rating, and per-variant specs and features (engine, safety, comfort, etc.).

**Single car:**
```bash
python scrapper.py --url https://www.carandbike.com/cars/maruti-suzuki/ertiga
```

**All cars from sitemap:**
```bash
python scrapper.py --sitemap https://www.carandbike.com/sitemap-cars.xml
```

**With options:**
```bash
python scrapper.py \
  --sitemap https://www.carandbike.com/sitemap-cars.xml \
  --max 10 \
  --max-price 40 \
  --delay 2 \
  --selenium \
  --outdir ./data
```

**Debug variant spec API for a car:**
```bash
python scrapper.py --test-api https://www.carandbike.com/maruti-suzuki-cars/e-vitara
```

**All flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--url URL` | — | Scrape a single car model page |
| `--sitemap URL` | — | Scrape all cars from a sitemap XML |
| `--test-api URL` | — | Debug variant spec API for one car |
| `--max N` | 0 (all) | Stop after N models |
| `--max-price N` | 40.0 | Skip cars priced above N lakh |
| `--delay N` | 2.0 | Seconds between requests |
| `--selenium` | off | Use headless Chrome (use when site returns 403) |
| `--output NAME` | auto | Output filename stem (no extension) |
| `--outdir PATH` | `.` | Directory to save output file |

**Output:** `carandbike_YYYYMMDD_HHMMSS.json`

**If you get 403 errors** from carandbike.com, add the `--selenium` flag. This launches headless Chrome via Selenium which bypasses bot detection. Requires Chrome to be installed on your machine.

**Auth token for variant specs:** Some variant spec API calls require an auth token from carandbike.com. The scraper tries to extract it automatically. If it fails, the terminal will show step-by-step instructions to copy it from Chrome DevTools and set it as the `CARANDBIKE_TOKEN` environment variable.

---

### acf_mapper.py

Converts the raw scraped JSON into a WordPress-ready ACF payload. Maps 130+ spec/feature names to their exact ACF field keys (`field_6a…`). Handles EV-specific fields (battery, charging, motor) automatically.

```bash
python acf_mapper.py carandbike_20260608_101806.json
```

**With options:**
```bash
python acf_mapper.py carandbike_20260608_101806.json \
  --output my_cars_acf.json \
  --first-variant-only
```

**All flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `input_json` | required | Raw JSON file from scrapper.py |
| `--output PATH` | `<stem>_acf.json` | Custom output path |
| `--first-variant-only` | off | Map only first variant per car (faster, smaller file) |

**Output:** `carandbike_20260608_101806_acf.json`

**What it does per model:**
- Builds model-level ACF fields (union of all variant specs — first value per key wins)
- Builds per-variant ACF fields (each variant gets its own full spec dict)
- Normalises Yes/No values (`available` → `Yes`, `not available` → `No`)
- Converts model + variant names to WordPress slugs

---

### wp_uploader.py

Reads the ACF JSON and pushes each car to WordPress via the REST API. For each car it:

1. Resolves or creates the **Brand Name** taxonomy term (e.g. `Maruti Suzuki`)
2. Resolves or creates the **Car Category** taxonomy term (e.g. `SUV`, `Sedan`)
3. Uploads gallery images to the Media Library (skips already-uploaded images)
4. Creates or updates the `cars-data` model post with both taxonomy terms assigned
5. Creates or updates one `car-variant` post per trim, linked via `parent_car`

Re-running is safe — the script checks slugs first and updates existing posts instead of creating duplicates.

```bash
python wp_uploader.py carandbike_20260608_101806_acf.json \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx"
```

**All flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `acf_json` | required | ACF JSON file from acf_mapper.py |
| `--wp-url URL` | required | WordPress site URL |
| `--user NAME` | required | WordPress username |
| `--password PASS` | required | WordPress Application Password |
| `--post-type SLUG` | `cars-data` | Model CPT slug |
| `--variant-post-type SLUG` | `car-variant` | Variant CPT slug |
| `--max N` | 0 (all) | Stop after N cars |
| `--dry-run` | off | Simulate — no data written |
| `--skip-images` | off | Skip gallery uploads (metadata-only sync) |
| `--skip-variants` | off | Upload model posts only, skip variants |
| `--max-gallery-images N` | 20 | Max images to upload per car |
| `--delay N` | 0.5 | Seconds between car uploads |
| `--variant-delay N` | 0.3 | Seconds between variant uploads |

**Examples:**

```bash
# Fast metadata-only sync (no images)
python wp_uploader.py cars_acf.json --wp-url ... --user ... --password ... --skip-images

# Upload models only, no variants
python wp_uploader.py cars_acf.json --wp-url ... --user ... --password ... --skip-variants

# Limit gallery to 5 images per car
python wp_uploader.py cars_acf.json --wp-url ... --user ... --password ... --max-gallery-images 5
```

---

### diagnose_acf.py

Diagnostic tool that checks your WordPress + ACF setup before running the full upload. Run this whenever ACF fields appear empty after upload.

```bash
python diagnose_acf.py \
  --wp-url https://yoursite.com \
  --user admin \
  --password "xxxx xxxx xxxx xxxx"
```

**What it checks:**

1. WordPress REST API connection and authentication
2. Whether the `ACF to REST API` or `ACF PRO` plugin is active
3. Whether `cars-data` CPT is accessible via REST and returns an `acf` key
4. Whether `car-variant` CPT is accessible and Variant Core Fields are readable
5. **Live write test** — creates a draft variant post, reads it back, verifies ACF values saved correctly, then deletes it
6. Whether each ACF field group has `show_in_rest` enabled

---

## Data Structure

### Raw JSON (scrapper.py output)

```json
{
  "brand": "Maruti Suzuki",
  "model": "Ertiga",
  "body_type": "MPV",
  "price_min_inr": 999000,
  "price_max_inr": 1599000,
  "price_min_display": "₹ 9.99 Lakh",
  "fuel_types": "Petrol, CNG",
  "rating": "4.2",
  "gallery_images": ["https://...jpg", "..."],
  "colours": [{"name": "Oxford Blue", "hex": "#..."}],
  "variants": [
    {
      "variant_name": "LXI MT",
      "variant_id": "12345",
      "price_inr": 999000,
      "price_display": "₹ 9.99 Lakh",
      "fuel_type": "Petrol",
      "transmission": "Manual",
      "mileage": "19.38 kmpl",
      "engine_cc": "1462",
      "specifications": [{"category": "Engine", "name": "Engine Type", "value": "K15C Smart Hybrid"}],
      "features": [{"category": "Safety", "name": "ABS", "value": "Yes"}]
    }
  ]
}
```

### ACF JSON (acf_mapper.py output)

```json
{
  "post_title": "Maruti Suzuki Ertiga",
  "slug": "maruti-suzuki-ertiga",
  "brand": "Maruti Suzuki",
  "body_type": "MPV",
  "acf": {
    "field_6a213ef1298c5": "K15C Smart Hybrid",
    "field_6a2149b4a571e": "Yes",
    "...": "..."
  },
  "variants": [
    {
      "variant_name": "LXI MT",
      "price_inr": 999000,
      "fuel_type": "Petrol",
      "transmission": "Manual",
      "mileage": "19.38 kmpl",
      "engine_cc": "1462",
      "acf": {
        "field_6a213ef1298c5": "K15C Smart Hybrid",
        "...": "..."
      }
    }
  ]
}
```

---

## WordPress Architecture

```
cars-data (post)          ←── brand-name taxonomy (e.g. "Maruti Suzuki")
  └── title: Maruti Suzuki Ertiga   ←── car-category taxonomy (e.g. "MPV")
  └── acf: gallery_images, colours_json, model-level specs
  └── meta: source_url, carandbike_model_id

car-variant (post) × N trims
  └── title: Maruti Suzuki Ertiga – LXI MT
  └── acf:
        parent_car    → [ID of cars-data post]   ← Relationship field
        variant_name  → "LXI MT"
        variant_price → "999000"
        price_display → "₹ 9.99 Lakh"
        fuel_type     → "Petrol"
        transmission  → "Manual"
        mileage       → "19.38 kmpl"
        engine_cc     → "1462"
        + all 130 spec/feature fields
```

### Taxonomy auto-creation

When the uploader encounters a new brand (e.g. `Tata`) or body type (e.g. `Electric`) it automatically creates the taxonomy term and assigns it to the post. On subsequent runs the existing term is reused.

---

## ACF Field Reference

### Variant Core Fields (group_6a2685967558f)

| Label | Field name | Field key |
|-------|-----------|-----------|
| Parent Car | `parent_car` | `field_6a268597de524` |
| Variant Name | `variant_name` | `field_6a2685efde525` |
| Variant Price | `variant_price` | `field_6a268600de526` |
| Price Display | `price_display` | `field_6a268615de527` |
| Fuel Type | `fuel_type` | `field_6a268622de528` |
| Transmission | `transmission` | `field_6a268626de529` |
| Mileage | `mileage` | `field_6a26862dde52a` |
| Engine CC | `engine_cc` | `field_6a268632de52b` |

### Battery Range and Charging — EV fields (group_6a278d9005bef)

| Label | Field name | Field key |
|-------|-----------|-----------|
| Battery Type | `battery_type` | `field_6a278d9183b5b` |
| Battery Capacity | `battery_capacity` | `field_6a278dac83b5c` |
| Charging Time | `charging_time` | `field_6a278db483b5d` |
| Charging Time AC Fast | `charging_time_ac_fast_charging` | `field_6a278dbd83b5e` |
| Battery Range | `battery_range` | `field_6a278dc683b5f` |

> All other field keys are defined in `SPEC_NAME_TO_ACF` inside `acf_mapper.py`. The uploader always sends data using field keys (`field_6a…`), never field names — this is required by the ACF REST API.

---

## Troubleshooting

### ACF fields are empty after upload

Most common cause: **ACF to REST API plugin is not active.**

Run the diagnostic:
```bash
python diagnose_acf.py --wp-url ... --user ... --password ...
```

Then check in order:
1. WP Admin → Plugins → confirm `ACF to REST API` is **Active**
2. ACF → Field Groups → edit each group → bottom of page → **Show in REST API → Yes**
3. Each field group must have a location rule: **Post Type = car-variant**

### 401 Authentication failed

Your Application Password is wrong or expired. Go to WP Admin → Users → Profile → Application Passwords → delete old one → create a new one.

### 404 Post type not found

The CPT is not registered with `show_in_rest: true`. In CPT UI, edit the `car-variant` post type and set **Show in REST API → True**.

### 403 on image download from carandbike.com

The site is blocking the request. Run scrapper with `--selenium` to use headless Chrome instead.

### Auth token not found (variant specs missing)

carandbike.com's variant spec API requires a token. The scraper tries to find it automatically. If it fails:

1. Open Chrome DevTools (F12) → Network tab → check **Preserve log**
2. Go to a car specifications page (e.g. `https://www.carandbike.com/maruti-suzuki-cars/ertiga/specifications`)
3. Click any variant tab
4. In Network tab filter for `variant-get-spec-features`
5. Click the request → Headers → copy the `Authorization` or `x-api-key` value
6. Set it as an environment variable and re-run:

```powershell
# PowerShell
$env:CARANDBIKE_TOKEN='<paste token here>'
python scrapper.py --url https://www.carandbike.com/cars/maruti-suzuki/ertiga
```

```bash
# macOS / Linux
export CARANDBIKE_TOKEN='<paste token here>'
python scrapper.py --url https://www.carandbike.com/cars/maruti-suzuki/ertiga
```

### Duplicate posts being created

The uploader matches by slug first, then by title. If the slug in the ACF JSON doesn't match the existing post's slug (e.g. because the post was created manually with a different slug), it will create a duplicate. Fix: either delete the manual post, or update its slug to match the auto-generated one (e.g. `maruti-suzuki-ertiga`).

---

## File Naming Convention

| File | Example |
|------|---------|
| Raw scraped data | `carandbike_20260608_101806.json` |
| ACF-mapped data | `carandbike_20260608_101806_acf.json` |

The timestamp in the filename is `YYYYMMDD_HHMMSS` from when the scrape was run. Always pass the `_acf.json` file (not the raw one) to `wp_uploader.py`.