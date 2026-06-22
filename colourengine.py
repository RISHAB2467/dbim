# =========================================================
# DBIM F-1 FUNDAMENTAL REQUIREMENTS AUTOMATION ENGINE
# Government Website Compliance Auditor
# =========================================================
#
# INSTALL:
#   pip install playwright requests beautifulsoup4 pandas numpy \
#               pillow imagehash opencv-python webcolors tinycss2 \
#               colormath nest_asyncio
#   playwright install
#
# RUN:
#   python dbim_f1_automation.py https://example.gov.in
#   python dbim_f1_automation.py
# =========================================================

import re
import sys
import json
import math
import time
import asyncio
import hashlib
import pathlib
import urllib.parse
import collections
import traceback
import subprocess
import tempfile
import os
import random
import ipaddress
from functools import lru_cache

import requests
import webcolors
import nest_asyncio
import numpy as np
import pandas as pd

from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse
from collections import defaultdict
from bs4 import BeautifulSoup

import cv2
import imagehash
from PIL import Image, ImageDraw, ImageFont

from playwright.async_api import async_playwright

from colormath.color_objects import sRGBColor, LabColor
from colormath.color_conversions import convert_color
from colormath.color_diff import delta_e_cie2000

nest_asyncio.apply()

# =========================================================
# CONFIGURATION - Centralized thresholds and settings
# =========================================================

class AuditConfig:
    """Centralized configuration for all audit thresholds and settings."""
    # Colour matching
    DELTA_E_THRESHOLD = 15.0        # Max ΔE for "close enough" colour match
    DELTA_E_EXACT = 2.0             # ΔE below this = visually identical
    
    # Accessibility
    MIN_CONTRAST_RATIO = 4.5        # WCAG AA for normal text / icons
    MIN_CONTRAST_LARGE = 3.0        # WCAG AA for large text
    
    # Icon constraints
    MAX_ICON_DIM = 128              # Pixels — larger = not an icon
    MIN_ICON_DIM = 8                # Pixels — smaller = decorative/spacer
    ASPECT_RATIO_TOLERANCE = 0.01   # Ratio diff considered "same"
    
    # Palette detection
    SECONDARY_RATIO_THRESHOLD = 0.4  # Above = palette inconsistency
    
    # Crawling
    MAX_PAGES = 100
    PAGE_TIMEOUT_MS = 30000
    SCROLL_STEPS = 5
    SCROLL_DELAY_MS = 400

CONFIG = AuditConfig()

# =========================================================
# BLOCKED HOSTS FOR SSRF PROTECTION
# =========================================================

BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0",
    "169.254.169.254",  # AWS metadata
    "metadata.google.internal",  # GCP metadata
    "metadata.cloudinit",  # cloud-init metadata
}

# =========================================================
# PALETTES - OFFICIAL DBIM COLOURS
# =========================================================

PRIMARY_GROUPS = {
    "Burgundy": ["#6C1340", "#A32966", "#DB70A6", "#EBADCC", "#FAEBF2"],
    "Purple":   ["#29136C", "#4729A3", "#6870DB", "#BDADEB", "#EEEBFA"],
    "Blue":     ["#162F6A", "#214AAB", "#5279D7", "#A3BBF3", "#D2DFFF"],
    "Green":    ["#0F5757", "#208686", "#75BDBD", "#A6D9D9", "#D9F2F2"],
    "Chrome Yellow": ["#503600", "#916100", "#DDA73A", "#F4D390", "#FFEECC"],
    "Cinnamon Red":  ["#771D1D", "#A72626", "#D75151", "#FCDADA", "#FFF0F0"],
}

# DBIM Official Functional/Semantic Colours
FUNCTIONAL = {
    "#198754": "success",   # Liberty Green - Status colour for success
    "#FFC107": "warning",   # Mustard Yellow - Status colour for warning
    "#DC3545": "error",     # Coral Red - Status colour for error
    "#0D6EFD": "info",      # Blue - Status colour for information & hyperlinks
}

# DBIM Official Neutral Colours
NEUTRALS = {
    "#FFFFFF",  # Inclusive White - Primary page background
    "#000000",  # Black - State Emblem on light background
    "#EBEAEA",  # Linen - Highlight images, quotes, box outlines
    "#150202",  # Deep Earthy Brown - Text on light backgrounds
    "#C6C6C6",  # Grey 01 - Functional grey colour 1
    "#8E8E8E",  # Grey 02 - Functional grey colour 2
    "#606060",  # Grey 03 - Functional grey colour 3
}

# Special Government Identity Colours
SPECIAL_GOVT = {
    "#1D0A69": "gov_in_root",  # Deep Blue - Gov.In root website identity
}

# Combined approved palette
ALL_APPROVED = []
for _g in PRIMARY_GROUPS.values():
    ALL_APPROVED.extend(_g)
ALL_APPROVED.extend(FUNCTIONAL.keys())
ALL_APPROVED.extend(NEUTRALS)
ALL_APPROVED.extend(SPECIAL_GOVT.keys())

ALLOWED_ICON_SIZES = {(24, 24), (32, 32), (48, 48), (64, 64)}
ALLOWED_ICON_FORMATS = {"png", "svg", "webp"}
DISALLOWED_ICON_FORMATS = {"jpg", "jpeg", "gif", "bmp", "ico", "tiff", "tif"}

UI_WEIGHTS = {
    "navbar": 10, "header": 10, "hero": 10, "button": 9,
    "footer": 8, "sidebar": 7, "card": 5, "text": 3,
    "border": 2, "shadow": 1,
}
PROPERTY_WEIGHTS = {
    "backgroundColor": 1.0, "color": 0.8, "borderColor": 0.5,
    "fill": 0.5, "stroke": 0.5,
}

HEX_RE = re.compile(r"#(?:[A-Fa-f0-9]{3}){1,2}")

# =========================================================
# OUTPUT DIRECTORIES
# =========================================================

REPORTS_DIR = Path("reports")
SCREENSHOTS_DIR = Path("screenshots")

for _req in [f"F1_{i:02d}" for i in range(1, 11)]:
    (SCREENSHOTS_DIR / _req).mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================
# SSRF PROTECTION
# =========================================================

def is_safe_url(url: str):
    """Return (is_safe, reason). Blocks SSRF targets."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""

        if host.lower() in BLOCKED_HOSTS:
            return False, f"Blocked host: {host}"

        # Block private IP ranges
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False, f"Private/internal IP blocked: {host}"
        except ValueError:
            pass  # Not an IP address, hostname is OK

        if parsed.scheme not in ("http", "https"):
            return False, f"Only HTTP/HTTPS allowed, got: {parsed.scheme}"

        return True, ""
    except Exception as e:
        return False, str(e)

# =========================================================
# COLOUR UTILITIES
# =========================================================

def normalize_hex(color):
    """Convert any CSS color value to uppercase hex string.
    Returns None for transparent, inherited, or unparseable values."""
    if not color:
        return None

    color = str(color).strip().lower()

    # Skip non-color keywords
    SKIP = {"transparent", "inherit", "unset", "none", "currentcolor",
            "initial", "revert", "revert-layer", ""}
    if color in SKIP:
        return None

    try:
        # Already a hex value
        if color.startswith("#"):
            return webcolors.normalize_hex(color).upper()

        # rgb() or rgba()
        rgba_match = re.match(
            r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)",
            color
        )
        if rgba_match:
            r, g, b = int(rgba_match[1]), int(rgba_match[2]), int(rgba_match[3])
            alpha = float(rgba_match[4]) if rgba_match[4] else 1.0
            if alpha == 0:
                return None  # Fully transparent
            return "#{:02X}{:02X}{:02X}".format(r, g, b)

        # hsl() - convert to rgb first
        hsl_match = re.match(
            r"hsla?\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%(?:\s*,\s*([\d.]+))?\s*\)",
            color
        )
        if hsl_match:
            h = float(hsl_match[1]) / 360
            s = float(hsl_match[2]) / 100
            l = float(hsl_match[3]) / 100
            alpha = float(hsl_match[4]) if hsl_match[4] else 1.0
            if alpha == 0:
                return None
            import colorsys
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            return "#{:02X}{:02X}{:02X}".format(
                int(r * 255), int(g * 255), int(b * 255)
            )

        # Named color (e.g., "red", "navy")
        try:
            return webcolors.name_to_hex(color).upper()
        except (ValueError, AttributeError):
            pass

    except Exception:
        pass

    return None


def hex_to_lab(hex_color):
    rgb = webcolors.hex_to_rgb(hex_color)
    srgb = sRGBColor(rgb.red / 255, rgb.green / 255, rgb.blue / 255)
    return convert_color(srgb, LabColor)


# Pre-compute Lab values for approved colours
_APPROVED_LAB = {}
for _hex in ALL_APPROVED:
    try:
        _APPROVED_LAB[_hex] = hex_to_lab(_hex)
    except Exception:
        pass


def delta_e(c1, c2):
    try:
        lab1 = hex_to_lab(c1)
        lab2 = hex_to_lab(c2)
        value = delta_e_cie2000(lab1, lab2)
        # Handle numpy scalar, numpy array, or plain float
        if isinstance(value, np.ndarray):
            value = value.flat[0]
        return float(value)
    except Exception:
        return 999.0


@lru_cache(maxsize=512)
def nearest_approved_colour(color, threshold=15):
    """Cached nearest colour lookup using pre-computed Lab values."""
    if color in ALL_APPROVED:
        return color, 0.0

    color_lab = None
    try:
        color_lab = hex_to_lab(color)
    except Exception:
        return None, 999.0

    best, best_dist = None, 999.0
    for approved, approved_lab in _APPROVED_LAB.items():
        try:
            d = float(delta_e_cie2000(color_lab, approved_lab))
            if d < best_dist:
                best_dist = d
                best = approved
        except Exception:
            continue

    if best_dist <= threshold:
        return best, best_dist
    return None, best_dist


def get_group(color):
    for name, colours in PRIMARY_GROUPS.items():
        if color in colours:
            return name
    return None


def luminance(hex_color):
    try:
        rgb = webcolors.hex_to_rgb(hex_color)
        vals = []
        for v in [rgb.red, rgb.green, rgb.blue]:
            v = v / 255
            vals.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]
    except Exception:
        return 0.0


def contrast_ratio(c1, c2):
    l1, l2 = luminance(c1), luminance(c2)
    light, dark = max(l1, l2), min(l1, l2)
    return (light + 0.05) / (dark + 0.05)


def classify_ui_area(class_name, tag):
    s = f"{class_name} {tag}".lower()
    for key in UI_WEIGHTS:
        if key in s:
            return key
    return "text"


def hex_to_rgb_tuple(hex_color):
    try:
        rgb = webcolors.hex_to_rgb(hex_color)
        return (rgb.red, rgb.green, rgb.blue)
    except Exception:
        return (0, 0, 0)

# =========================================================
# SCREENSHOT HELPER WITH ANNOTATION
# =========================================================

_screenshot_counters = defaultdict(int)

def _next_shot_path(req_id: str, label: str = "item") -> Path:
    _screenshot_counters[req_id] += 1
    idx = _screenshot_counters[req_id]
    folder = SCREENSHOTS_DIR / req_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{label}_{idx:03d}.png"


def _safe_int(v):
    try:
        return int(round(float(v)))
    except Exception:
        return 0


async def capture_annotated_screenshot(
    page,
    req_id: str,
    label: str,
    rect: dict = None,
    note: str = "",
    selector: str = None,
) -> str:
    """
    Capture a screenshot with a red box + label on the exact issue area.
    If selector is provided, it will scroll that element into view and use
    its bounding box. If rect is provided, it will annotate that rect.
    """
    path = _next_shot_path(req_id, label)

    try:
        # Resolve target rectangle
        box = None

        if selector:
            try:
                el = page.locator(selector).first
                await el.scroll_into_view_if_needed()
                box = await el.bounding_box()
            except Exception:
                box = None

        if box:
            rect = {
                "x": box.get("x", 0),
                "y": box.get("y", 0),
                "width": box.get("width", 0),
                "height": box.get("height", 0),
            }

        if not rect:
            # fallback to plain screenshot if no coordinates are available
            await page.screenshot(path=str(path), full_page=False)
            return str(path)

        x = _safe_int(rect.get("x", 0))
        y = _safe_int(rect.get("y", 0))
        w = max(1, _safe_int(rect.get("width", rect.get("w", 0))))
        h = max(1, _safe_int(rect.get("height", rect.get("h", 0))))

        # Scroll target into view if it appears off-screen
        try:
            await page.evaluate(
                f"window.scrollTo({{ top: Math.max(0, {y} - 100), behavior: 'instant' }})"
            )
            await page.wait_for_timeout(300)
        except Exception:
            pass

        # Re-fetch rect after scroll if we used a selector
        if selector:
            try:
                el = page.locator(selector).first
                box = await el.bounding_box()
                if box:
                    x = _safe_int(box.get("x", 0))
                    y = _safe_int(box.get("y", 0))
                    w = max(1, _safe_int(box.get("width", 0)))
                    h = max(1, _safe_int(box.get("height", 0)))
            except Exception:
                pass

        raw_path = path.with_name(path.stem + "_raw.png")
        await page.screenshot(path=str(raw_path), full_page=False)

        img = Image.open(raw_path).convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        img_w, img_h = img.size

        # Clamp coordinates to image bounds
        x1 = max(0, min(img_w - 1, x))
        y1 = max(0, min(img_h - 1, y))
        x2 = max(0, min(img_w - 1, x + w))
        y2 = max(0, min(img_h - 1, y + h))

        # Ensure box is visible (minimum 4px)
        if x2 - x1 < 4:
            x2 = min(img_w - 1, x1 + 4)
        if y2 - y1 < 4:
            y2 = min(img_h - 1, y1 + 4)

        # Red translucent highlight + thick border
        draw.rectangle(
            [x1, y1, x2, y2],
            outline=(255, 0, 0, 255),
            fill=(255, 0, 0, 45),
            width=4,
        )

        # Add corner markers for extra visibility
        marker_size = 8
        for cx, cy in [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]:
            draw.rectangle(
                [cx - marker_size, cy - marker_size, cx + marker_size, cy + marker_size],
                fill=(255, 0, 0, 255),
                outline=(255, 255, 255, 255),
                width=2,
            )

        # Label with arrow
        if note:
            try:
                font = ImageFont.truetype("arial.ttf", 18)
            except Exception:
                try:
                    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
                except Exception:
                    font = ImageFont.load_default()

            tb = draw.textbbox((0, 0), note, font=font)
            tw = tb[2] - tb[0]
            th = tb[3] - tb[1]

            pad = 8
            tx = x1
            ty = max(0, y1 - th - 20)

            # If label would go off top, place it below the box
            if ty < 5:
                ty = min(img_h - th - pad * 2 - 5, y2 + 10)

            # If label would go off right, shift left
            if tx + tw + pad * 2 > img_w:
                tx = max(0, img_w - tw - pad * 2 - 2)

            bg_box = [
                tx, 
                ty, 
                min(img_w - 1, tx + tw + pad * 2), 
                min(img_h - 1, ty + th + pad * 2)
            ]
            # White background with red border
            draw.rectangle(bg_box, fill=(255, 255, 255, 240), outline=(255, 0, 0, 255), width=3)
            draw.text((bg_box[0] + pad, bg_box[1] + pad), note, fill=(200, 0, 0, 255), font=font)

            # Arrow from label to box
            label_cx = (bg_box[0] + bg_box[2]) // 2
            label_cy = bg_box[3] if ty < y1 else bg_box[1]
            target_cx = (x1 + x2) // 2
            target_cy = y1 if ty < y1 else y2

            draw.line([(label_cx, label_cy), (target_cx, target_cy)], 
                     fill=(255, 0, 0, 255), width=3)
            
            # Arrow head
            import math
            angle = math.atan2(target_cy - label_cy, target_cx - label_cx)
            arrow_len = 12
            ax1 = target_cx - arrow_len * math.cos(angle - math.pi / 6)
            ay1 = target_cy - arrow_len * math.sin(angle - math.pi / 6)
            ax2 = target_cx - arrow_len * math.cos(angle + math.pi / 6)
            ay2 = target_cy - arrow_len * math.sin(angle + math.pi / 6)
            draw.polygon(
                [(target_cx, target_cy), (ax1, ay1), (ax2, ay2)],
                fill=(255, 0, 0, 255),
            )

        img.save(str(path), "PNG")
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:
            pass

        return str(path)

    except Exception as e:
        print(f"    [annotation error] {e}")
        try:
            await page.screenshot(path=str(path), full_page=False)
        except Exception:
            return ""
        return str(path)


async def capture_page_screenshot(page, req_id: str, label: str = "page") -> str:
    path = _next_shot_path(req_id, label)
    try:
        await page.screenshot(path=str(path), full_page=False)
    except Exception:
        return ""
    return str(path)


async def capture_element_screenshot(page, selector: str, req_id: str, label: str = "violation") -> str:
    """Legacy function - uses annotated screenshot now."""
    try:
        el = page.locator(selector).first
        await el.scroll_into_view_if_needed()
        box = await el.bounding_box()
        if box:
            return await capture_annotated_screenshot(
                page, req_id, label,
                rect=box,
                note=label.replace("_", " ").title()
            )
    except Exception:
        pass
    return await capture_page_screenshot(page, req_id, label)

# =========================================================
# CRAWLING ENGINE
# =========================================================

MAX_PAGES = 100


def _same_domain(base_url: str, candidate: str) -> bool:
    """Check if candidate is same domain or subdomain of base_url."""
    try:
        def root_domain(netloc: str) -> str:
            # Strip port, strip www prefix
            host = netloc.split(":")[0].lower()
            if host.startswith("www."):
                host = host[4:]
            return host

        base_root = root_domain(urlparse(base_url).netloc)
        cand_root = root_domain(urlparse(candidate).netloc)

        # Allow exact match OR subdomain of base
        return (
            cand_root == base_root
            or cand_root.endswith("." + base_root)
        )
    except Exception:
        return False


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


async def crawl_website(start_url: str) -> list:
    visited = set()
    queue = [start_url]
    pages = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-setuid-sandbox", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )

        # Try sitemap
        sitemap_urls = _fetch_sitemap_urls(start_url)
        for su in sitemap_urls:
            if _same_domain(start_url, su) and su not in visited:
                queue.append(su)

        while queue and len(pages) < MAX_PAGES:
            url = queue.pop(0)
            url = _normalize_url(url)
            if url in visited:
                continue
            visited.add(url)
            print(f"  Crawling [{len(pages)+1}/{MAX_PAGES}]: {url}")

            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # Scroll to trigger lazy loading
                await page.evaluate(
                    """
                    async () => {
                        for (let i = 0; i < 5; i++) {
                            window.scrollBy(0, window.innerHeight);
                            await new Promise(r => setTimeout(r, 400));
                        }
                        window.scrollTo(0, 0);
                    }
                    """
                )
                await page.wait_for_timeout(1000)

                html = await page.content()
                pages.append({"url": url, "page": page, "html": html})

                # Collect links
                links = await page.evaluate(
                    """
                    () => {
                        return [...document.querySelectorAll('a[href]')]
                            .map(a => a.href)
                            .filter(h => h && !h.startsWith('javascript:') && !h.startsWith('mailto:'));
                    }
                    """
                )
                for link in links:
                    norm = _normalize_url(link)
                    if _same_domain(start_url, norm) and norm not in visited:
                        queue.append(norm)

            except Exception as e:
                print(f"    Failed to crawl {url}: {e}")
                try:
                    await page.close()
                except Exception:
                    pass
                continue

        await browser.close()

    return pages


def _fetch_sitemap_urls(base_url: str) -> list:
    urls = []
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        try:
            resp = requests.get(urljoin(base_url, path), timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "xml")
                for loc in soup.find_all("loc"):
                    urls.append(loc.text.strip())
        except Exception:
            pass
    return urls

# =========================================================
# STYLE EXTRACTION
# =========================================================

EXTRACT_STYLES_JS = """
() => {
    try {
        const elements = [...document.querySelectorAll('*')];
        const data = [];
        elements.forEach(el => {
            try {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                data.push({
                    tag: el.tagName || "",
                    className: typeof el.className === 'string' ? el.className : "",
                    id: el.id || "",
                    text: (el.innerText || "").substring(0, 200),
                    styles: {
                        color: style.color || "",
                        backgroundColor: style.backgroundColor || "",
                        borderColor: style.borderColor || "",
                        fill: style.fill || "",
                        stroke: style.stroke || "",
                        backgroundImage: style.backgroundImage || "",
                    },
                    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                });
            } catch(e) {}
        });
        return data;
    } catch(e) { return []; }
}
"""


async def extract_rendered_styles(page) -> list:
    try:
        result = await page.evaluate(EXTRACT_STYLES_JS)
        return result or []
    except Exception:
        return []


def extract_colours_from_elements(elements: list) -> list:
    extracted = []
    for el in elements:
        ui_area = classify_ui_area(el.get("className", ""), el.get("tag", ""))
        for prop, val in el.get("styles", {}).items():
            if prop == "backgroundImage":
                continue
            colour = normalize_hex(val)
            if not colour:
                continue
            extracted.append({
                "colour": colour,
                "property": prop,
                "ui_area": ui_area,
                "weight": UI_WEIGHTS.get(ui_area, 1),
                "property_weight": PROPERTY_WEIGHTS.get(prop, 1),
                "text": el.get("text", ""),
                "tag": el.get("tag", ""),
                "className": el.get("className", ""),
            })
    return extracted

# =========================================================
# DOMINANT GROUP DETECTION
# =========================================================

def detect_dominant_group(colours: list) -> dict:
    scores = defaultdict(float)
    rogue = []

    for item in colours:
        c = item["colour"]
        if c in FUNCTIONAL or c in NEUTRALS:
            continue
        nearest, dist = nearest_approved_colour(c)
        if not nearest:
            rogue.append(c)
            continue
        group = get_group(nearest)
        if not group:
            continue
        scores[group] += item["weight"] * item["property_weight"]

    if not scores:
        return {
            "dominant_group": "UNKNOWN",
            "scores": {},
            "secondary_ratio": 1.0,
            "confidence": 0.0,
            "rogue_candidates": rogue,
        }

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    dominant = sorted_scores[0][0]
    dominant_score = sorted_scores[0][1]
    secondary_ratio = (sorted_scores[1][1] / dominant_score) if len(sorted_scores) > 1 else 0.0
    confidence = dominant_score / sum(scores.values()) if sum(scores.values()) > 0 else 0.0

    return {
        "dominant_group": dominant,
        "scores": dict(scores),
        "secondary_ratio": secondary_ratio,
        "confidence": round(confidence, 3),
        "rogue_candidates": list(set(rogue)),
    }

# =========================================================
# CSS EXTRACTION
# =========================================================

def extract_css_text(url: str) -> str:
    try:
        html = requests.get(url, timeout=15).text
    except Exception:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    css_text = ""
    for tag in soup.find_all("style"):
        css_text += tag.get_text() + "\n"
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        href = link.get("href")
        if href:
            full = urljoin(url, href)
            try:
                css_text += requests.get(full, timeout=10).text + "\n"
            except Exception:
                pass
    return css_text


def extract_gradients(css_text: str) -> list:
    gradients = []
    pattern = r"(linear-gradient|radial-gradient|conic-gradient)\(([^)]+)\)"
    for m in re.finditer(pattern, css_text, re.IGNORECASE):
        gradients.append(m.group(2))
    return gradients


def extract_gradient_colours(gradient: str) -> list:
    found = HEX_RE.findall(gradient)
    return [normalize_hex(x) for x in found if normalize_hex(x)]

# =========================================================
# F1-01: PRIMARY COLOUR PALETTE
# =========================================================

async def check_f1_01(page, url: str, colours: list, dominant_result: dict) -> dict:
    status = "PASS"
    reason = ""
    evidence = ""

    dg = dominant_result["dominant_group"]
    conf = dominant_result["confidence"]
    sec_ratio = dominant_result["secondary_ratio"]

    approved_groups = list(PRIMARY_GROUPS.keys())

    # ✅ Get actual dominant HEX (first shade of group)
    key_hex = PRIMARY_GROUPS.get(dg, [None])[0]

    if dg == "UNKNOWN":
        status = "FAIL"
        reason = "No dominant primary colour group detected on the website."
        key_hex = None

    elif dg not in approved_groups:
        status = "FAIL"
        reason = f"Dominant colour group '{dg}' is not in the DBIM approved primary palette."

    elif sec_ratio > CONFIG.SECONDARY_RATIO_THRESHOLD:
        status = "FAIL"
        reason = (
            f"Dominant group '{dg}' (confidence {conf:.1%}) is significantly competing with "
            f"another group (secondary ratio {sec_ratio:.2f}). Palette inconsistency detected."
        )

    else:
        reason = (
            f"Dominant colour group '{dg}' detected with confidence {conf:.1%}. "
            f"Primary HEX: {key_hex}. Compliant."
        )

    # For F1-01, try to annotate the header/hero area which most represents the primary palette
    if status == "FAIL":
        # Try to annotate the most prominent element (header/hero)
        try:
            header_box = await page.evaluate("""
                () => {
                    const el = document.querySelector('header, .header, .hero, .banner, nav, .navbar');
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                }
            """)
            if header_box:
                evidence = await capture_annotated_screenshot(
                    page, "F1_01", "primary_palette_violation",
                    rect=header_box,
                    note=f"Primary palette issue: {dg}"
                )
            else:
                evidence = await capture_page_screenshot(page, "F1_01", "primary_palette")
        except Exception:
            evidence = await capture_page_screenshot(page, "F1_01", "primary_palette")
    else:
        evidence = await capture_page_screenshot(page, "F1_01", "primary_palette")

    return {
        "requirement": "F1-01",
        "name": "Primary Colour Palette",
        "status": status,
        "reason": reason,
        "actual": key_hex,
        "expected": ", ".join(approved_groups),
        "evidence": evidence,
    }

# =========================================================
# F1-02: FUNCTIONAL COLOUR PALETTE
# =========================================================

async def check_f1_02(page, url: str, colours: list) -> list:

    js_code = """
    (function() {

        function normaliseHex(colour) {
            if (!colour) return null;
            var rgbMatch = colour.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
            if (rgbMatch) {
                var a = rgbMatch[4] !== undefined ? parseFloat(rgbMatch[4]) : 1;
                if (a === 0) return null;
                var r = parseInt(rgbMatch[1]);
                var g = parseInt(rgbMatch[2]);
                var b = parseInt(rgbMatch[3]);
                return '#' + [r, g, b].map(function(v) {
                    return v.toString(16).padStart(2, '0');
                }).join('').toUpperCase();
            }
            if (colour.startsWith('#')) {
                var hex = colour.replace('#', '').toUpperCase();
                if (hex.length === 3) {
                    hex = hex.split('').map(function(c) { return c + c; }).join('');
                }
                return '#' + hex;
            }
            return null;
        }

        function isIgnoredColour(hex, property) {
            if (!hex) return true;
            var upper = hex.toUpperCase();
            
            if (upper === 'TRANSPARENT') return true;
            
            var r = parseInt(upper.slice(1, 3), 16);
            var g = parseInt(upper.slice(3, 5), 16);
            var b = parseInt(upper.slice(5, 7), 16);
            if (property === 'background-color' && r > 245 && g > 245 && b > 245) return true;
            
            if (property === 'color' && r < 50 && g < 50 && b < 50) return true;
            
            if ((property === 'border-color' || property === 'outline-color')) {
                var avg = (r + g + b) / 3;
                if (Math.abs(r - avg) < 10 && Math.abs(g - avg) < 10 && Math.abs(b - avg) < 10) {
                    if (avg > 100 && avg < 200) return true;
                }
            }
            
            return false;
        }

        function isVisible(el) {
            try {
                var style = window.getComputedStyle(el);
                var rect  = el.getBoundingClientRect();
                return (
                    style.display    !== 'none'   &&
                    style.visibility !== 'hidden' &&
                    style.opacity    !== '0'      &&
                    rect.width        > 0         &&
                    rect.height       > 0
                );
            } catch(e) { return false; }
        }

        function getFrameworkRootColours() {
            var frameworkColours = [];
            try {
                var rootStyles = getComputedStyle(document.documentElement);
                var bsVars = [
                    '--bs-primary', '--bs-secondary', '--bs-success',
                    '--bs-info', '--bs-warning', '--bs-danger',
                    '--bs-light', '--bs-dark', '--bs-body-color',
                    '--bs-body-bg', '--bs-link-color', '--bs-link-hover-color',
                    '--bs-border-color', '--bs-heading-color'
                ];
                bsVars.forEach(function(varName) {
                    var val = rootStyles.getPropertyValue(varName).trim();
                    if (val) {
                        var hex = normaliseHex(val);
                        if (hex && frameworkColours.indexOf(hex) === -1) {
                            frameworkColours.push(hex);
                        }
                    }
                });
            } catch(e) {}
            return frameworkColours;
        }

        function classifySemanticColour(hex) {
            if (!hex) return null;
            
            var dbimFunctional = {
                '#198754': 'success',
                '#FFC107': 'warning',
                '#DC3545': 'error',
                '#0D6EFD': 'info',
                '#0DCAF0': 'info'
            };
            
            if (dbimFunctional[hex]) {
                return dbimFunctional[hex];
            }
            
            var r = parseInt(hex.slice(1, 3), 16);
            var g = parseInt(hex.slice(3, 5), 16);
            var b = parseInt(hex.slice(5, 7), 16);
            var rN = r / 255, gN = g / 255, bN = b / 255;
            var max = Math.max(rN, gN, bN);
            var min = Math.min(rN, gN, bN);
            var l   = (max + min) / 2;
            var h = 0, s = 0;
            if (max !== min) {
                var d = max - min;
                s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
                if      (max === rN) h = ((gN - bN) / d + (gN < bN ? 6 : 0)) / 6;
                else if (max === gN) h = ((bN - rN) / d + 2) / 6;
                else                 h = ((rN - gN) / d + 4) / 6;
            }
            var hDeg = Math.round(h * 360);
            var sPct = Math.round(s * 100);
            var lPct = Math.round(l * 100);
            
            if (sPct < 40) return null;
            if (lPct < 15 || lPct > 85) return null;
            
            if (hDeg >= 100 && hDeg <= 150 && sPct > 50) return 'success';
            if (hDeg >= 45  && hDeg <= 70  && sPct > 60) return 'warning';
            if ((hDeg >= 0 && hDeg <= 15) || (hDeg >= 345 && hDeg <= 360)) {
                if (sPct > 50) return 'error';
            }
            
            return null;
        }

        function getSelector(el) {
            var sel = el.tagName.toLowerCase();
            if (el.id) sel += '#' + el.id;
            if (el.className && typeof el.className === 'string') {
                var classes = el.className.trim().split(/\\s+/).slice(0, 2);
                if (classes[0]) sel += '.' + classes.join('.');
            }
            return sel;
        }

        var UI_SELECTORS = [
            'button', '.btn',
            'input[type="button"]', 'input[type="submit"]', 'input[type="reset"]',
            '[role="button"]',
            'a', 'nav a', '.nav-link',
            'nav', '.navbar', '.nav', '.navigation',
            'header', '.header', '.site-header',
            '.menu', '.menu-item', '.dropdown-menu', '.dropdown-item',
            '.cta', '.call-to-action',
            '.tab', '.tabs', '[role="tab"]',
            '.badge', '.alert', '[role="alert"]',
            '.card', '.card-header', '.card-footer',
            '.pagination', '.page-item', '.page-link',
            'svg', 'svg path', 'svg circle', 'svg rect',
            '.icon', '[class*="icon"]',
            '[class*="btn"]', '[class*="button"]',
            '[class*="primary"]', '[class*="secondary"]'
        ];

        var PROPERTIES = [
            { css: 'backgroundColor', label: 'background-color' },
            { css: 'color',           label: 'color'            },
            { css: 'borderTopColor',  label: 'border-color'     },
            { css: 'fill',            label: 'fill'             },
            { css: 'stroke',          label: 'stroke'           }
        ];

        var frameworkColours = getFrameworkRootColours();
        var colourData = {};
        var seen = [];
        var elements = [];

        function elInSeen(el) {
            for (var i = 0; i < seen.length; i++) {
                if (seen[i] === el) return true;
            }
            return false;
        }

        UI_SELECTORS.forEach(function(selector) {
            try {
                var found = document.querySelectorAll(selector);
                for (var i = 0; i < found.length; i++) {
                    var el = found[i];
                    if (!elInSeen(el)) {
                        seen.push(el);
                        elements.push({ el: el, selector: selector });
                    }
                }
            } catch(e) {}
        });

        function recordColour(hex, elSelector, property) {
            if (!hex) return;
            if (isIgnoredColour(hex, property)) return;
            if (!colourData[hex]) {
                colourData[hex] = { count: 0, selectors: [], properties: [] };
            }
            colourData[hex].count++;
            if (colourData[hex].selectors.indexOf(elSelector) === -1) {
                colourData[hex].selectors.push(elSelector);
            }
            if (colourData[hex].properties.indexOf(property) === -1) {
                colourData[hex].properties.push(property);
            }
        }

        elements.forEach(function(item) {
            var el = item.el;
            if (!isVisible(el)) return;
            try {
                var computed   = window.getComputedStyle(el);
                var elSelector = getSelector(el);

                PROPERTIES.forEach(function(prop) {
                    var value = computed.getPropertyValue(prop.css);
                    if (!value) return;
                    value = value.trim();
                    if (!value ||
                        value === 'inherit'      ||
                        value === 'currentColor' ||
                        value === 'initial'      ||
                        value === 'unset'        ||
                        value === 'transparent'  ||
                        value === 'none') return;
                    var hex = normaliseHex(value);
                    if (hex) recordColour(hex, elSelector, prop.label);
                });

                var tagName = el.tagName ? el.tagName.toLowerCase() : '';
                if (tagName === 'svg' || tagName === 'path' ||
                    tagName === 'circle' || tagName === 'rect' ||
                    el.closest('svg')) {
                    ['fill', 'stroke'].forEach(function(attr) {
                        var val = el.getAttribute(attr);
                        if (val && val !== 'none' && val !== 'currentColor') {
                            var hex = normaliseHex(val);
                            if (hex) recordColour(hex, elSelector, attr);
                        }
                    });
                }
            } catch(e) {}
        });

        var sorted = Object.keys(colourData).sort(function(a, b) {
            return colourData[b].count - colourData[a].count;
        });

        var interactiveColours = [];
        var semanticColours    = {};

        sorted.forEach(function(hex) {
            var data = colourData[hex];
            var sem  = classifySemanticColour(hex);
            if (sem) {
                if (!semanticColours[sem]) {
                    semanticColours[sem] = {
                        hex:        hex,
                        count:      data.count,
                        selectors:  data.selectors.slice(0, 5),
                        properties: data.properties
                    };
                }
            } else {
                interactiveColours.push({
                    hex:        hex,
                    count:      data.count,
                    selectors:  data.selectors.slice(0, 5),
                    properties: data.properties
                });
            }
        });

        return {
            interactiveColours:   interactiveColours,
            semanticColours:      semanticColours,
            frameworkColours:     frameworkColours,
            totalElementsScanned: elements.length
        };

    })()
    """

    try:
        data = await page.evaluate(js_code)
    except Exception as e:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "ERROR",
            "reason": f"F1-02 scan failed during browser execution: {str(e)}",
            "actual": "N/A",
            "expected": "Functional colours from rendered UI elements",
            "evidence": "",
        }]

    if not data:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "ERROR",
            "reason": "F1-02: No data returned from browser scan.",
            "actual": "N/A",
            "expected": "Functional colours from rendered UI elements",
            "evidence": "",
        }]

    interactive        = data.get("interactiveColours", [])
    semantic           = data.get("semanticColours", {})
    framework_excluded = data.get("frameworkColours", [])
    total_scanned      = data.get("totalElementsScanned", 0)

    primary   = interactive[0] if len(interactive) > 0 else None
    secondary = interactive[1] if len(interactive) > 1 else None

    # Try to find and annotate a button/link with the primary functional color
    if primary:
        try:
            primary_hex = primary['hex']
            element_box = await page.evaluate(f"""
                () => {{
                    const targetHex = '{primary_hex}';
                    const buttons = document.querySelectorAll('button, .btn, a, [role="button"]');
                    for (const el of buttons) {{
                        const style = getComputedStyle(el);
                        const bg = style.backgroundColor;
                        // Match against rgb format
                        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {{
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 20 && rect.height > 20) {{
                                return {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height }};
                            }}
                        }}
                    }}
                    return null;
                }}
            """)
            if element_box:
                evidence = await capture_annotated_screenshot(
                    page, "F1_02", "functional_palette",
                    rect=element_box,
                    note=f"Primary functional: {primary['hex']}"
                )
            else:
                evidence = await capture_page_screenshot(page, "F1_02", "functional_palette")
        except Exception:
            evidence = await capture_page_screenshot(page, "F1_02", "functional_palette")
    else:
        evidence = await capture_page_screenshot(page, "F1_02", "functional_palette")

    if not primary and total_scanned == 0:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "WARN",
            "reason": (
                "No visible interactive UI elements found. "
                "Could not determine functional colour palette from rendered elements."
            ),
            "actual": "0 elements scanned",
            "expected": "Functional colours from rendered UI elements",
            "evidence": evidence,
        }]

    if not primary:
        return [{
            "requirement": "F1-02",
            "name": "Functional Colour Palette",
            "status": "WARN",
            "reason": (
                "Functional colour palette could not be determined. "
                "No significant interactive colours detected beyond white/black."
            ),
            "actual": f"{total_scanned} elements scanned, no qualifying colours found",
            "expected": "Functional colours from rendered UI elements",
            "evidence": evidence,
        }]

    reason_lines = [
        f"Primary functional colour {primary['hex']} detected "
        f"from {primary['count']} rendered UI elements.",
    ]

    if secondary:
        reason_lines.append(
            f"Secondary functional colour {secondary['hex']} detected "
            f"from {secondary['count']} rendered UI elements."
        )

    if semantic:
        reason_lines.append("Semantic colours detected:")
        for sem_type, sem_data in semantic.items():
            reason_lines.append(
                f"  {sem_type}: {sem_data['hex']} ({sem_data['count']} elements)"
            )

    if framework_excluded:
        reason_lines.append(
            f"Excluded {len(framework_excluded)} Bootstrap/framework default colour(s): "
            + ", ".join(framework_excluded)
        )

    return [{
        "requirement": "F1-02",
        "name": "Functional Colour Palette",
        "status": "PASS",
        "reason": "\n".join(reason_lines),
        "actual": (
            f"Primary: {primary['hex']} ({primary['count']} elements)"
            + (f" | Secondary: {secondary['hex']} ({secondary['count']} elements)" if secondary else "")
        ),
        "expected": "Functional colours detected from rendered UI elements (not framework defaults)",
        "evidence": evidence,
    }]

# =========================================================
# F1-03: ICON COLOURS
# =========================================================

ICON_SELECTORS = "svg, img, i, use, symbol, [class*='icon'], [class*='fa-'], [class*='bi-'], [class*='material']"


async def check_f1_03(page, url: str, dominant_result: dict) -> list:
    results = []
    dominant_group = dominant_result["dominant_group"]
    dominant_palette = PRIMARY_GROUPS.get(dominant_group, [])
    approved_icon_colours = set(dominant_palette) | {"#FFFFFF"}

    icons = await page.evaluate(
        """
        () => {
            const selectors = ['svg', 'img[src*="icon"]', 'img[src*="svg"]',
                               'i[class*="fa"]', 'i[class*="bi"]', 'i[class*="material"]',
                               '[class*="icon"]'];
            const found = [];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;
                    const style = getComputedStyle(el);
                    found.push({
                        tag: el.tagName,
                        className: typeof el.className === 'string' ? el.className : "",
                        src: el.src || el.getAttribute('href') || "",
                        fill: el.getAttribute('fill') || style.fill || "",
                        stroke: el.getAttribute('stroke') || style.stroke || "",
                        color: style.color || "",
                        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                    });
                });
            });
            return found;
        }
        """
    )

    if not icons:
        evidence = await capture_page_screenshot(page, "F1_03", "no_icons")
        return [{
            "requirement": "F1-03",
            "name": "Icon Colours",
            "status": "PASS",
            "reason": "No icons detected on the page.",
            "actual": "N/A",
            "expected": f"Dominant key colour or #FFFFFF",
            "evidence": evidence,
        }]

    for idx, icon in enumerate(icons[:30]):
        raw_colours = [icon.get("fill"), icon.get("stroke"), icon.get("color")]
        # FIX: Use parentheses to fix operator precedence - allow #FFFFFF which is in NEUTRALS
        detected = [c for c in raw_colours if c and (c not in NEUTRALS or c == "#FFFFFF")]
        detected = [normalize_hex(c) for c in detected if normalize_hex(c)]

        if not detected:
            continue

        for c in detected:
            if not c:
                continue
            nearest, dist = nearest_approved_colour(c)
            if nearest in approved_icon_colours or dist <= 10:
                pass
            else:
                # Capture UNIQUE annotated screenshot for THIS specific violation
                rect_data = icon.get("rect", {})
                evidence = await capture_annotated_screenshot(
                    page,
                    "F1_03",
                    f"icon_colour_violation_{idx + 1}",
                    rect=rect_data,
                    note=f"Wrong icon colour: {c}"
                )
                results.append({
                    "requirement": "F1-03",
                    "name": "Icon Colours",
                    "status": "FAIL",
                    "reason": f"Icon uses colour {c} which is not the dominant key colour or #FFFFFF. Nearest approved: {nearest} (ΔE={dist:.1f})",
                    "actual": c,
                    "expected": f"Dominant group colour or #FFFFFF",
                    "evidence": evidence,
                })

    if not results:
        evidence = await capture_page_screenshot(page, "F1_03", "icons_compliant")
        results.append({
            "requirement": "F1-03",
            "name": "Icon Colours",
            "status": "PASS",
            "reason": f"All detected icons use approved colours (dominant group: {dominant_group} or #FFFFFF).",
            "actual": dominant_group,
            "expected": f"{dominant_group} or #FFFFFF",
            "evidence": evidence,
        })

    return results

# =========================================================
# F1-04: FOOTER BACKGROUND COLOUR
# =========================================================

async def check_f1_04(page, url: str, dominant_result: dict) -> dict:

    dominant_group = dominant_result["dominant_group"]
    dominant_palette = PRIMARY_GROUPS.get(dominant_group, [])
    key_colour = dominant_palette[0] if dominant_palette else None

    # STEP 1: Aggressive multi-pass scrolling to force-render lazy footers
    try:
        await page.evaluate("""
            async () => {
                // Multi-pass scroll: top -> bottom -> bottom again
                window.scrollTo(0, 0);
                await new Promise(r => setTimeout(r, 300));

                const totalHeight = Math.max(
                    document.body.scrollHeight,
                    document.documentElement.scrollHeight
                );
                const step = window.innerHeight;
                for (let y = 0; y < totalHeight; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 200));
                }

                // Final settle at very bottom
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, 500));
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise(r => setTimeout(r, 500));
            }
        """)
        await page.wait_for_timeout(1000)
    except Exception:
        pass

    # STEP 2: Multi-strategy footer detection (semantic -> class/id -> text-based -> bottom-element)
    footer_data = await page.evaluate(
        """
        () => {
            // ============================================================
            // FOOTER KEYWORDS - very broad list for text-based detection
            // ============================================================
            const FOOTER_KEYWORDS = [
                'useful links', 'quick links', 'related links', 'important links',
                'archives', 'archive',
                'contact us', 'contact', 'get in touch', 'reach us',
                'sitemap', 'site map', 'site-map',
                'last updated', 'last modified', 'page last updated', 'last reviewed',
                'copyright', '©', '(c)',
                'all rights reserved',
                'privacy policy', 'terms of use', 'terms and conditions',
                'disclaimer', 'hyperlinking policy',
                'help', 'accessibility', 'accessibility statement',
                'follow us', 'social media', 'connect with us',
                'powered by', 'designed by', 'developed by', 'maintained by',
                'nic', 'national informatics centre',
                'visitor count', 'visitors', 'total visitors',
                'feedback', 'rti', 'right to information',
                'website policies', 'web information manager'
            ];

            // ============================================================
            // HELPER: parse rgb()/rgba() to uppercase hex
            // ============================================================
            function rgbToHexLocal(rgb) {
                if (!rgb) return null;
                if (rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
                const m = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                if (!m) return null;
                const a = m[4] !== undefined ? parseFloat(m[4]) : 1;
                if (a === 0) return null;
                const r = parseInt(m[1]), g = parseInt(m[2]), b = parseInt(m[3]);
                return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
            }

            function isNearWhite(hex) {
                if (!hex) return true;
                const r = parseInt(hex.slice(1, 3), 16);
                const g = parseInt(hex.slice(3, 5), 16);
                const b = parseInt(hex.slice(5, 7), 16);
                return (r > 240 && g > 240 && b > 240);
            }

            function countFooterKeywords(el) {
                if (!el) return 0;
                const text = (el.innerText || '').toLowerCase();
                if (text.length < 5) return 0;
                let hits = 0;
                for (const kw of FOOTER_KEYWORDS) {
                    if (text.includes(kw)) hits++;
                }
                return hits;
            }

            // Walk up element to find its first non-transparent visible background
            function getEffectiveBg(el) {
                let cur = el;
                let depth = 0;
                while (cur && depth < 6) {
                    const style = window.getComputedStyle(cur);
                    const bg = style.backgroundColor;
                    const hex = rgbToHexLocal(bg);
                    if (hex) {
                        return { hex: hex, sourceEl: cur, sourceTag: cur.tagName.toLowerCase() };
                    }
                    cur = cur.parentElement;
                    depth++;
                }
                return null;
            }

            // ============================================================
            // STRATEGY 1: Try semantic and common class/id selectors
            // ============================================================
            const SELECTORS = [
                'footer[role="contentinfo"]',
                'footer',
                '[role="contentinfo"]',
                '#footer',
                '.footer',
                '.site-footer',
                '.page-footer',
                '.main-footer',
                '.footer-wrapper',
                '.footer-section',
                '.footer-container',
                '.footer-area',
                '.footer-content',
                '.bottom-footer',
                '.global-footer',
                '#site-footer',
                '#main-footer',
                '#footer-wrapper',
                '[class*="footer"]',
                '[id*="footer"]',
                '[class*="Footer"]',
                '[id*="Footer"]'
            ];

            const docHeight = Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight
            );

            const candidates = [];
            const seenElements = new Set();

            for (const sel of SELECTORS) {
                let elList = [];
                try {
                    elList = document.querySelectorAll(sel);
                } catch (e) { continue; }

                for (const el of elList) {
                    if (seenElements.has(el)) continue;
                    seenElements.add(el);

                    const rect = el.getBoundingClientRect();
                    const absTop = rect.top + window.scrollY;
                    const absBottom = rect.bottom + window.scrollY;

                    if (rect.width < 100 || rect.height < 20) continue;
                    if (rect.height > 5000) continue;

                    candidates.push({
                        el: el,
                        selector: sel,
                        strategy: 'semantic',
                        rect: rect,
                        absTop: absTop,
                        absBottom: absBottom,
                        keywordHits: countFooterKeywords(el)
                    });
                }
            }

            // ============================================================
            // STRATEGY 2: Text-based search - find any container holding
            // multiple footer keywords (works for custom <div> footers)
            // ============================================================
            const allContainers = document.querySelectorAll(
                'div, section, aside, nav, ul, table'
            );
            for (const el of allContainers) {
                if (seenElements.has(el)) continue;

                const rect = el.getBoundingClientRect();
                if (rect.width < 200 || rect.height < 30) continue;
                if (rect.height > 5000) continue;

                const absBottom = rect.bottom + window.scrollY;
                // Must be in bottom half of document
                if (absBottom < docHeight * 0.5) continue;

                const hits = countFooterKeywords(el);
                if (hits >= 2) {
                    seenElements.add(el);
                    candidates.push({
                        el: el,
                        selector: el.tagName.toLowerCase() +
                                  (el.id ? '#' + el.id : '') +
                                  (el.className && typeof el.className === 'string'
                                      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.')
                                      : ''),
                        strategy: 'text-keyword',
                        rect: rect,
                        absTop: rect.top + window.scrollY,
                        absBottom: absBottom,
                        keywordHits: hits
                    });
                }
            }

            // ============================================================
            // STRATEGY 3: Bottom-of-page fallback. Walk up from the
            // last visible element to find an enclosing container with
            // meaningful height and a coloured background.
            // ============================================================
            if (candidates.length === 0) {
                window.scrollTo(0, docHeight);
                const x = window.innerWidth / 2;
                const y = Math.min(window.innerHeight - 10, docHeight - 10);
                let el = document.elementFromPoint(x, y);
                let depth = 0;
                while (el && depth < 10) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 300 && rect.height > 60 && rect.height < 3000) {
                        candidates.push({
                            el: el,
                            selector: 'bottom-fallback:' + el.tagName.toLowerCase(),
                            strategy: 'bottom-fallback',
                            rect: rect,
                            absTop: rect.top + window.scrollY,
                            absBottom: rect.bottom + window.scrollY,
                            keywordHits: countFooterKeywords(el)
                        });
                        break;
                    }
                    el = el.parentElement;
                    depth++;
                }
            }

            if (candidates.length === 0) {
                return { found: false, reason: 'No footer candidates from any strategy' };
            }

            // ============================================================
            // SCORE all candidates and pick the best
            // ============================================================
            let best = null;
            let bestScore = -Infinity;

            for (const c of candidates) {
                const bgInfo = getEffectiveBg(c.el);
                let score = 0;

                // Keyword hits dominate the score
                score += c.keywordHits * 40;

                // Position: closer to the bottom = better
                const distFromBottom = docHeight - c.absBottom;
                if (distFromBottom < 50)  score += 60;
                else if (distFromBottom < 200) score += 40;
                else if (distFromBottom < docHeight * 0.2) score += 25;
                else if (distFromBottom < docHeight * 0.4) score += 10;

                // Semantic <footer> tag bonus
                if (c.el.tagName && c.el.tagName.toLowerCase() === 'footer') score += 50;

                // Bonus for class/id containing "footer"
                const idCls = ((c.el.id || '') + ' ' + (typeof c.el.className === 'string' ? c.el.className : '')).toLowerCase();
                if (idCls.includes('footer')) score += 30;

                // Non-white background bonus
                if (bgInfo && !isNearWhite(bgInfo.hex)) score += 35;

                // Reasonable height bonus
                if (c.rect.height >= 100 && c.rect.height <= 1500) score += 15;

                // Penalty if very tiny
                if (c.rect.height < 50) score -= 30;

                // Strategy bonus (semantic > text > fallback)
                if (c.strategy === 'semantic') score += 10;
                else if (c.strategy === 'text-keyword') score += 20;
                else if (c.strategy === 'bottom-fallback') score -= 10;

                if (score > bestScore) {
                    bestScore = score;
                    best = { candidate: c, bgInfo: bgInfo, score: score };
                }
            }

            if (!best) {
                return { found: false, reason: 'No candidate could be scored' };
            }

            // If chosen element has no usable bg, try a child with a real bg
            if (!best.bgInfo) {
                const inner = best.candidate.el.querySelectorAll('*');
                for (const child of inner) {
                    const childBg = getEffectiveBg(child);
                    if (childBg && !isNearWhite(childBg.hex)) {
                        best.bgInfo = childBg;
                        break;
                    }
                }
            }

            // Scroll the chosen footer into view so the screenshot captures it
            try {
                best.candidate.el.scrollIntoView({ block: 'center', behavior: 'instant' });
            } catch (e) {
                try { best.candidate.el.scrollIntoView(); } catch (e2) {}
            }

            const finalRect = best.candidate.el.getBoundingClientRect();

            return {
                found: true,
                backgroundColor: best.bgInfo ? best.bgInfo.hex : null,
                selector: best.candidate.selector,
                strategy: best.candidate.strategy,
                keywordHits: best.candidate.keywordHits,
                score: best.score,
                candidateCount: candidates.length,
                rect: {
                    x: finalRect.x,
                    y: finalRect.y,
                    width: finalRect.width,
                    height: finalRect.height
                }
            };
        }
        """
    )

    await page.wait_for_timeout(600)

    if not footer_data or not footer_data.get("found"):
        evidence = await capture_page_screenshot(page, "F1_04", "footer_not_found")
        reason_extra = ""
        if footer_data:
            reason_extra = f" ({footer_data.get('reason', 'unknown')})"
        return {
            "requirement": "F1-04",
            "name": "Footer Background Colour",
            "status": "FAIL",
            "reason": (
                "Could not detect any footer-like region on the page after "
                "scrolling, semantic search, text-keyword search, and "
                "bottom-element fallback." + reason_extra
            ),
            "actual": "N/A",
            "expected": key_colour,
            "evidence": evidence,
        }

    bg = normalize_hex(footer_data.get("backgroundColor"))
    rect_data = footer_data.get("rect")
    strategy = footer_data.get("strategy", "unknown")
    selector_used = footer_data.get("selector", "unknown")
    kw_hits = footer_data.get("keywordHits", 0)
    score = footer_data.get("score", 0)
    cand_count = footer_data.get("candidateCount", 0)

    debug_suffix = (
        f" [strategy={strategy}, selector='{selector_used}', "
        f"keywords={kw_hits}, score={score}, candidates_evaluated={cand_count}]"
    )

    # Determine pass/fail
    if not bg:
        status = "FAIL"
        reason = (
            "Footer region was located but no usable background colour could be parsed."
            + debug_suffix
        )
        actual = "no-bg"
    elif bg == key_colour:
        status = "PASS"
        reason = f"Footer background {bg} matches dominant key colour {key_colour}." + debug_suffix
        actual = bg
    else:
        dist = delta_e(bg, key_colour) if key_colour else 999
        if dist <= CONFIG.DELTA_E_THRESHOLD:
            status = "PASS"
            reason = (
                f"Footer background {bg} is visually equivalent to {key_colour} "
                f"(ΔE={dist:.1f})." + debug_suffix
            )
            actual = bg
        else:
            status = "FAIL"
            reason = (
                f"Footer background {bg} does not match dominant key colour {key_colour}."
                + debug_suffix
            )
            actual = bg

    # Annotate ONLY the actual footer bounding box
    note_text = f"Footer BG: {bg if bg else 'unknown'}"
    if status == "FAIL" and key_colour and bg:
        note_text = f"Wrong Footer BG: {bg} (expected {key_colour})"
    elif status == "FAIL":
        note_text = f"Footer detected but BG unreadable"

    if rect_data and rect_data.get("width", 0) > 0 and rect_data.get("height", 0) > 0:
        evidence = await capture_annotated_screenshot(
            page, "F1_04", "footer_detected",
            rect=rect_data,
            note=note_text
        )
    else:
        evidence = await capture_page_screenshot(page, "F1_04", "footer_detected")

    return {
        "requirement": "F1-04",
        "name": "Footer Background Colour",
        "status": status,
        "reason": reason,
        "actual": actual,
        "expected": key_colour,
        "evidence": evidence,
    }

# =========================================================
# F1-05: CONSISTENT ICON STYLE
# =========================================================

async def check_f1_05(page, url: str) -> dict:
    icon_images = await page.evaluate(
        """
        () => {
            const icons = [];
            document.querySelectorAll('svg, img[src*="icon"], img[src*="svg"]').forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width < 8 || rect.height < 8 || rect.width > 100 || rect.height > 100) return;
                icons.push({
                    tag: el.tagName,
                    src: el.src || "",
                    className: typeof el.className === 'string' ? el.className : "",
                    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                });
            });
            return icons;
        }
        """
    )

    if not icon_images or len(icon_images) < 2:
        evidence = await capture_page_screenshot(page, "F1_05", "icon_style")
        return {
            "requirement": "F1-05",
            "name": "Consistent Icon Style",
            "status": "PASS",
            "reason": "Fewer than 2 icons detected; consistency check skipped.",
            "actual": f"{len(icon_images or [])} icons",
            "expected": "Consistent style",
            "evidence": evidence,
        }

    style_counts = defaultdict(int)
    style_examples = defaultdict(list)
    style_patterns = {
        "outline": ["outline", "-o-", "-regular"],
        "filled": ["filled", "solid", "-fill", "-fas"],
        "rounded": ["round", "rounded"],
        "sharp": ["sharp", "angular"],
        "flat": ["flat", "simple"],
    }

    for icon in icon_images:
        cls = icon.get("className", "").lower()
        src = icon.get("src", "").lower()
        combined = cls + " " + src
        matched = False
        for style_name, patterns in style_patterns.items():
            if any(p in combined for p in patterns):
                style_counts[style_name] += 1
                style_examples[style_name].append(icon)
                matched = True
                break
        if not matched:
            style_counts["unknown"] += 1
            style_examples["unknown"].append(icon)

    dominant_styles = [k for k, v in style_counts.items() if v > 0 and k != "unknown"]
    mixed = len(dominant_styles) > 1

    if mixed:
        # Find an icon from the less common style to annotate
        sorted_styles = sorted(dominant_styles, key=lambda k: style_counts[k])
        less_common_style = sorted_styles[0]
        example_icon = style_examples[less_common_style][0]
        
        evidence = await capture_annotated_screenshot(
            page, "F1_05", "icon_style_mixed",
            rect=example_icon.get("rect"),
            note=f"Inconsistent style: {less_common_style}"
        )
        
        return {
            "requirement": "F1-05",
            "name": "Consistent Icon Style",
            "status": "FAIL",
            "reason": f"Mixed icon styles detected: {dict(style_counts)}.",
            "actual": str(dict(style_counts)),
            "expected": "Single consistent icon style",
            "evidence": evidence,
        }

    evidence = await capture_page_screenshot(page, "F1_05", "icon_consistency")
    return {
        "requirement": "F1-05",
        "name": "Consistent Icon Style",
        "status": "PASS",
        "reason": f"Icon styles appear consistent: {dict(style_counts)}.",
        "actual": str(dict(style_counts)),
        "expected": "Single consistent icon style",
        "evidence": evidence,
    }

# =========================================================
# F1-06: DBIM ICON SET USAGE
# =========================================================

async def check_f1_06(page, url: str) -> dict:

    icon_data = await page.evaluate(
        """
        () => {
            const icons = [];
            document.querySelectorAll(
                'svg, svg use, img[src*="icon"], i[class], [class*="icon"]'
            ).forEach(el => {

                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;

                const cls  = typeof el.className === 'string' ? el.className : "";
                const src  = el.src || "";
                const href = el.getAttribute('href') || el.getAttribute('xlink:href') || "";

                icons.push({
                    tag: el.tagName,
                    className: cls,
                    src: src,
                    href: href,
                    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                });
            });

            return icons;
        }
        """
    )

    if not icon_data:
        evidence = await capture_page_screenshot(page, "F1_06", "dbim_icon_usage")
        return {
            "requirement": "F1-06",
            "name": "DBIM Icon Set Usage",
            "status": "PASS",
            "reason": "No icons detected on the page.",
            "actual": "0 icons found",
            "expected": "Icons from DBIM Toolkit",
            "evidence": evidence,
        }

    total = len(icon_data)

    NON_DBIM_PATTERNS = [
        "fa-",
        "fas ", "far ", "fab ",
        "bi-",
        "material",
        "mdi-",
        "ion-",
        "feather",
    ]

    non_dbim_count = 0
    non_dbim_list = []
    non_dbim_examples = []

    for icon in icon_data:
        combined = (
            (icon.get("className", "") or "") + " " +
            (icon.get("src", "") or "") + " " +
            (icon.get("href", "") or "")
        ).lower()

        for pattern in NON_DBIM_PATTERNS:
            if pattern in combined:
                non_dbim_count += 1
                non_dbim_list.append(combined[:80])
                non_dbim_examples.append({"icon": icon, "pattern": pattern.strip()})
                break

    if non_dbim_count == 0:
        status = "PASS"
        reason = (
            f"{total} icons detected. No external icon libraries "
            f"(FontAwesome/Bootstrap/Material/etc.) detected. "
            f"Icons likely sourced from DBIM Toolkit or custom government set."
        )
        actual = "No external libraries detected"
        evidence = await capture_page_screenshot(page, "F1_06", "dbim_icon_usage")

    else:
        match_pct = ((total - non_dbim_count) / total) * 100

        if match_pct >= 70:
            status = "PASS"
            reason = (
                f"{match_pct:.1f}% of icons do not match known external libraries. "
                f"Minor usage of non‑DBIM icons detected."
            )
            evidence = await capture_page_screenshot(page, "F1_06", "dbim_icon_usage")
        else:
            status = "FAIL"
            reason = (
                f"Significant usage of external icon libraries detected "
                f"({non_dbim_count}/{total}). "
                f"Examples: {non_dbim_list[:3]}"
            )
            # Annotate first non-DBIM icon example
            example = non_dbim_examples[0]
            evidence = await capture_annotated_screenshot(
                page, "F1_06", "non_dbim_icon",
                rect=example["icon"].get("rect"),
                note=f"Non-DBIM icon: {example['pattern']}"
            )

        actual = f"{match_pct:.1f}% likely DBIM"

    return {
        "requirement": "F1-06",
        "name": "DBIM Icon Set Usage",
        "status": status,
        "reason": reason,
        "actual": actual,
        "expected": "Icons selected from DBIM Toolkit (no external icon libraries)",
        "evidence": evidence,
    }

# =========================================================
# F1-07: ICON FILE FORMAT
# =========================================================

async def check_f1_07(page, url: str) -> list:
    results = []

    icon_srcs = await page.evaluate(
        """
        () => {
            const srcs = [];
            document.querySelectorAll('img').forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && rect.width <= 128 && rect.height <= 128) {
                    srcs.push({ 
                        src: el.src || "", 
                        alt: el.alt || "", 
                        width: rect.width, 
                        height: rect.height, 
                        x: rect.x, 
                        y: rect.y 
                    });
                }
            });
            return srcs;
        }
        """
    )

    if not icon_srcs:
        evidence = await capture_page_screenshot(page, "F1_07", "icon_format")
        return [{
            "requirement": "F1-07",
            "name": "Icon File Format",
            "status": "PASS",
            "reason": "No small image icons detected.",
            "actual": "N/A",
            "expected": "PNG, SVG, or WEBP",
            "evidence": evidence,
        }]

    violations = []
    for icon in icon_srcs:
        src = icon.get("src", "")
        if not src:
            continue
        ext = src.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext in DISALLOWED_ICON_FORMATS:
            violations.append({"src": src, "ext": ext, "icon": icon})

    if violations:
        for i, v in enumerate(violations[:10], start=1):
            icon = v["icon"]
            rect_data = {
                "x": icon.get("x", 0),
                "y": icon.get("y", 0),
                "width": icon.get("width", 0),
                "height": icon.get("height", 0)
            }
            evidence = await capture_annotated_screenshot(
                page,
                "F1_07",
                f"format_violation_{v['ext']}_{i}",
                rect=rect_data,
                note=f"Disallowed format: .{v['ext']}"
            )
            results.append({
                "requirement": "F1-07",
                "name": "Icon File Format",
                "status": "FAIL",
                "reason": f"Icon uses disallowed format '.{v['ext']}': {v['src'][:80]}",
                "actual": f".{v['ext']}",
                "expected": "PNG, SVG, or WEBP",
                "evidence": evidence,
            })
    else:
        evidence = await capture_page_screenshot(page, "F1_07", "icon_format_pass")
        results.append({
            "requirement": "F1-07",
            "name": "Icon File Format",
            "status": "PASS",
            "reason": f"All {len(icon_srcs)} detected icons use approved formats (PNG/SVG/WEBP).",
            "actual": "PNG/SVG/WEBP",
            "expected": "PNG, SVG, or WEBP",
            "evidence": evidence,
        })

    return results

# =========================================================
# F1-08: ICON SIZE
# =========================================================

async def check_f1_08(page, url: str) -> list:
    results = []

    icon_data = await page.evaluate(
        """
        () => {

            function isSquare(w, h) {
                return Math.abs(w - h) <= 3;
            }

            const icons = [];

            document.querySelectorAll(
                'svg, i[class*="icon"], i[class*="fa"], i[class*="bi"], [class*="icon"]'
            ).forEach(el => {

                const rect = el.getBoundingClientRect();

                if (rect.width < 12 || rect.height < 12) return;
                if (rect.width > 80 || rect.height > 80) return;

                if (!isSquare(rect.width, rect.height)) return;

                icons.push({
                    renderedWidth: Math.round(rect.width),
                    renderedHeight: Math.round(rect.height),
                    x: rect.x,
                    y: rect.y
                });
            });

            return icons;
        }
        """
    )

    if not icon_data:
        evidence = await capture_page_screenshot(page, "F1_08", "icon_size")
        return [{
            "requirement": "F1-08",
            "name": "Icon Size",
            "status": "PASS",
            "reason": "No qualifying UI icons detected for size validation.",
            "actual": "N/A",
            "expected": "24x24, 32x32, 48x48, or 64x64",
            "evidence": evidence,
        }]

    ALLOWED = {(24, 24), (32, 32), (48, 48), (64, 64)}

    violations = []

    for icon in icon_data:
        w = icon["renderedWidth"]
        h = icon["renderedHeight"]

        matched = any(
            abs(w - aw) <= 3 and abs(h - ah) <= 3
            for aw, ah in ALLOWED
        )

        if not matched:
            violations.append(icon)

    if violations:
        # Group by unique size and capture annotated screenshot for each unique violation
        unique_sizes = list({(v["renderedWidth"], v["renderedHeight"]) for v in violations})[:10]

        for i, (w, h) in enumerate(unique_sizes, start=1):
            # Find first violation with this size
            v = next(v for v in violations if v["renderedWidth"] == w and v["renderedHeight"] == h)
            rect_data = {
                "x": v["x"], 
                "y": v["y"], 
                "width": v["renderedWidth"], 
                "height": v["renderedHeight"]
            }
            evidence = await capture_annotated_screenshot(
                page,
                "F1_08",
                f"size_violation_{w}x{h}_{i}",
                rect=rect_data,
                note=f"Wrong size: {w}x{h}px"
            )
            results.append({
                "requirement": "F1-08",
                "name": "Icon Size",
                "status": "FAIL",
                "reason": f"Icon at ({v['x']:.0f}, {v['y']:.0f}) has non-DBIM size {w}x{h}px.",
                "actual": f"{w}x{h}",
                "expected": "24x24, 32x32, 48x48, or 64x64",
                "evidence": evidence,
            })

        return results

    evidence = await capture_page_screenshot(page, "F1_08", "icon_size_pass")
    return [{
        "requirement": "F1-08",
        "name": "Icon Size",
        "status": "PASS",
        "reason": f"All {len(icon_data)} UI icons match approved DBIM sizes.",
        "actual": "Compliant",
        "expected": "24x24, 32x32, 48x48, or 64x64",
        "evidence": evidence,
    }]

# =========================================================
# F1-09: ICON ASPECT RATIO
# =========================================================

async def check_f1_09(page, url: str) -> list:
    """
    F1-09: ICON ASPECT RATIO
    Manual Requirement:
        'The correct proportion of icon is retained and icon
         is not compressed or stretched'
    """
    results = []

    try:
        icon_data = await page.evaluate(
            """
            () => {
                const icons = [];

                document.querySelectorAll('img, svg').forEach(el => {
                    try {
                        const rect = el.getBoundingClientRect();

                        // Skip invisible
                        if (rect.width < 8 || rect.height < 8) return;
                        if (rect.width > 500 || rect.height > 500) return;

                        const style = window.getComputedStyle(el);
                        if (style.display === 'none')      return;
                        if (style.visibility === 'hidden') return;
                        if (style.opacity === '0')         return;

                        const objectFit = (style.objectFit || 'fill').toLowerCase();

                        // SVG viewBox + preserveAspectRatio
                        let vbWidth = 0, vbHeight = 0;
                        let svgPreserveAR = '';
                        if (el.tagName.toLowerCase() === 'svg') {
                            const vb = el.getAttribute('viewBox');
                            if (vb) {
                                const parts = vb.trim().split(/[ \t,]+/);
                                if (parts.length >= 4) {
                                    vbWidth  = parseFloat(parts[2]);
                                    vbHeight = parseFloat(parts[3]);
                                }
                            }
                            svgPreserveAR = el.getAttribute('preserveAspectRatio') || '';
                        }

                        icons.push({
                            tag:            el.tagName.toLowerCase(),
                            src:            el.src || el.getAttribute('href') || '',
                            alt:            el.getAttribute('alt') || '',
                            renderedWidth:  Math.round(rect.width),
                            renderedHeight: Math.round(rect.height),
                            naturalWidth:   el.naturalWidth  || 0,
                            naturalHeight:  el.naturalHeight || 0,
                            viewBoxWidth:   vbWidth,
                            viewBoxHeight:  vbHeight,
                            objectFit:      objectFit,
                            svgPreserveAR:  svgPreserveAR,
                            cssWidth:       style.width  || '',
                            cssHeight:      style.height || '',
                            x:              rect.x,
                            y:              rect.y
                        });
                    } catch(e) {}
                });

                return icons;
            }
            """
        )
    except Exception as e:
        print(f"  [F1-09] JavaScript evaluation failed: {e}")
        icon_data = []

    if not icon_data:
        evidence = await capture_page_screenshot(page, "F1_09", "no_icons")
        return [{
            "requirement": "F1-09",
            "name":        "Icon Aspect Ratio",
            "status":      "PASS",
            "reason":      "No icons detected on the page.",
            "actual":      "N/A",
            "expected":    "Icon proportion retained (not compressed or stretched).",
            "evidence":    evidence,
        }]

    violations = []
    passed     = []
    seen_srcs  = set()

    for icon in icon_data:
        rw = icon.get("renderedWidth",  0)
        rh = icon.get("renderedHeight", 0)
        if rw <= 0 or rh <= 0:
            continue

        src        = icon.get("src", "").strip()
        object_fit = icon.get("objectFit", "fill")
        tag        = icon.get("tag", "")

        if src and src in seen_srcs:
            continue
        if src:
            seen_srcs.add(src)

        rendered_ratio = rw / rh

        # RULE 1: object-fit guarantees proportion is retained
        if object_fit in ("contain", "cover", "scale-down", "none"):
            passed.append({
                **icon,
                "reason_pass": f"object-fit: {object_fit} preserves proportion"
            })
            continue

        # RULE 2: SVG default behavior preserves aspect ratio
        if tag == "svg":
            svg_par = icon.get("svgPreserveAR", "").strip().lower()
            if svg_par != "none":
                passed.append({
                    **icon,
                    "reason_pass": "SVG default preserveAspectRatio preserves proportion"
                })
                continue

        # RULE 3: Compare intrinsic vs rendered ratio
        nw = icon.get("naturalWidth",  0) or icon.get("viewBoxWidth",  0)
        nh = icon.get("naturalHeight", 0) or icon.get("viewBoxHeight", 0)

        if nw > 0 and nh > 0:
            intrinsic_ratio = nw / nh

            if round(intrinsic_ratio, 2) == round(rendered_ratio, 2):
                passed.append({
                    **icon,
                    "intrinsic_ratio": round(intrinsic_ratio, 2),
                    "rendered_ratio":  round(rendered_ratio,  2),
                    "reason_pass":     "Ratios match"
                })
            else:
                if rendered_ratio > intrinsic_ratio:
                    deformation = "stretched horizontally / compressed vertically"
                else:
                    deformation = "compressed horizontally / stretched vertically"

                violations.append({
                    **icon,
                    "intrinsic_ratio": round(intrinsic_ratio, 2),
                    "rendered_ratio":  round(rendered_ratio,  2),
                    "deformation":     deformation,
                })
            continue

        # No size info — cannot determine, treat as pass
        passed.append({
            **icon,
            "reason_pass": "No intrinsic size available (cannot verify, assumed OK)"
        })

    # BUILD RESULTS - UNIQUE annotated screenshot per violation
    if violations:
        for i, v in enumerate(violations[:10], start=1):
            tag = v.get("tag", "").upper()
            intrinsic_ratio = v.get("intrinsic_ratio", "?")
            rendered_ratio = v.get("rendered_ratio", "?")
            deformation = v.get("deformation", "")
            nw = v.get("naturalWidth", 0) or v.get("viewBoxWidth", 0)
            nh = v.get("naturalHeight", 0) or v.get("viewBoxHeight", 0)
            rw = v.get("renderedWidth", 0)
            rh = v.get("renderedHeight", 0)
            src = v.get("src", "N/A")
            css_w = v.get("cssWidth", "")
            css_h = v.get("cssHeight", "")
            obj_fit = v.get("objectFit", "fill")

            rect_data = {
                "x": v.get("x", 0),
                "y": v.get("y", 0),
                "width": rw,
                "height": rh,
            }

            # Capture UNIQUE annotated screenshot for THIS violation
            evidence = await capture_annotated_screenshot(
                page,
                "F1_09",
                f"aspect_ratio_violation_{i}",
                rect=rect_data,
                note=f"Ratio mismatch: {rendered_ratio} vs {intrinsic_ratio}"
            )

            reason = (
                f"{tag} proportion NOT retained — icon is {deformation}. "
                f"Original: {nw}x{nh} (ratio {intrinsic_ratio}) | "
                f"Rendered: {rw}x{rh}px (ratio {rendered_ratio}). "
                f"CSS: width={css_w}, height={css_h}, object-fit={obj_fit}. "
                f"Source: {src}"
            )

            results.append({
                "requirement": "F1-09",
                "name":        "Icon Aspect Ratio",
                "status":      "FAIL",
                "reason":      reason,
                "actual":      f"Original {intrinsic_ratio} → Rendered {rendered_ratio}",
                "expected":    "Original ratio = Rendered ratio (proportion retained)",
                "evidence":    evidence,
            })

    else:
        evidence = await capture_page_screenshot(
            page, "F1_09", "aspect_ratio_pass"
        )
        results.append({
            "requirement": "F1-09",
            "name":        "Icon Aspect Ratio",
            "status":      "PASS",
            "reason":      (
                f"All {len(passed)} icons retain correct proportion. "
                f"No compression or stretching detected."
            ),
            "actual":      f"{len(passed)} icons checked, all proportions retained",
            "expected":    "Icon proportion retained (not compressed or stretched).",
            "evidence":    evidence,
        })

    print(f"\n  [F1-09] Total checked : {len(passed) + len(violations)}")
    print(f"  [F1-09] Passed        : {len(passed)}")
    print(f"  [F1-09] Failed        : {len(violations)}")
    print(f"  [F1-09] Result        : {'FAIL' if violations else 'PASS'}")

    return results

# =========================================================
# F1-10: ICON CONTRAST ON IMAGES
# =========================================================

async def check_f1_10(page, url: str) -> list:
    results = []

    overlay_icons = await page.evaluate(
        """
        () => {
            const found = [];
            const imgContainers = document.querySelectorAll(
                '[class*="hero"], [class*="banner"], [class*="slider"], [class*="carousel"], [class*="card"]'
            );
            imgContainers.forEach(container => {
                container.querySelectorAll('svg, i, [class*="icon"]').forEach(icon => {
                    const rect = icon.getBoundingClientRect();
                    if (rect.width < 4 || rect.height < 4) return;
                    const style = getComputedStyle(icon);
                    const contStyle = getComputedStyle(container);
                    found.push({
                        iconColor: style.color || style.fill || "#000000",
                        containerBg: contStyle.backgroundColor || contStyle.background || "#FFFFFF",
                        containerBgImage: contStyle.backgroundImage || "",
                        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                    });
                });
            });
            return found;
        }
        """
    )

    if not overlay_icons:
        evidence = await capture_page_screenshot(page, "F1_10", "icon_contrast")
        return [{
            "requirement": "F1-10",
            "name": "Icon Contrast on Images",
            "status": "PASS",
            "reason": "No icons detected over image/banner backgrounds.",
            "actual": "N/A",
            "expected": "Contrast ratio >= 4.5:1",
            "evidence": evidence,
        }]

    for idx, item in enumerate(overlay_icons[:20], start=1):
        fg_raw = normalize_hex(item.get("iconColor", ""))
        bg_raw = normalize_hex(item.get("containerBg", ""))
        has_bg_img = "url(" in item.get("containerBgImage", "")

        if not fg_raw:
            fg_raw = "#000000"
        if not bg_raw or has_bg_img:
            bg_raw = "#FFFFFF"

        try:
            ratio = contrast_ratio(fg_raw, bg_raw)
        except Exception:
            ratio = 0.0

        # Capture UNIQUE annotated screenshot for THIS icon
        icon_rect = item.get("rect", {})
        rect_data = {
            "x": icon_rect.get("x", 0),
            "y": icon_rect.get("y", 0),
            "width": icon_rect.get("width", icon_rect.get("w", 0)),
            "height": icon_rect.get("height", icon_rect.get("h", 0)),
        }

        if ratio < CONFIG.MIN_CONTRAST_RATIO:
            note = f"Low contrast: {ratio:.2f}:1 (need 4.5:1)"
            evidence = await capture_annotated_screenshot(
                page, "F1_10", f"contrast_fail_{idx}",
                rect=rect_data,
                note=note
            )
            results.append({
                "requirement": "F1-10",
                "name": "Icon Contrast on Images",
                "status": "FAIL",
                "reason": f"Icon colour {fg_raw} on background {bg_raw} has contrast ratio {ratio:.2f}:1 (required >= 4.5:1).",
                "actual": f"{ratio:.2f}:1",
                "expected": ">= 4.5:1",
                "evidence": evidence,
            })
        else:
            note = f"Contrast OK: {ratio:.2f}:1"
            evidence = await capture_annotated_screenshot(
                page, "F1_10", f"contrast_pass_{idx}",
                rect=rect_data,
                note=note
            )
            results.append({
                "requirement": "F1-10",
                "name": "Icon Contrast on Images",
                "status": "PASS",
                "reason": f"Icon colour {fg_raw} on background {bg_raw} meets contrast ratio {ratio:.2f}:1.",
                "actual": f"{ratio:.2f}:1",
                "expected": ">= 4.5:1",
                "evidence": evidence,
            })

    if not results:
        evidence = await capture_page_screenshot(page, "F1_10", "icon_contrast_pass")
        results.append({
            "requirement": "F1-10",
            "name": "Icon Contrast on Images",
            "status": "PASS",
            "reason": "No contrast violations detected on icon-over-image elements.",
            "actual": "N/A",
            "expected": ">= 4.5:1",
            "evidence": evidence,
        })

    return results

# =========================================================
# SCORING
# =========================================================

def score_results(all_results: list) -> dict:
    """Score results with proper handling of WARN and ERROR statuses."""
    countable = [r for r in all_results if r.get("status") in ("PASS", "FAIL")]
    total  = len(countable)
    passed = sum(1 for r in countable if r["status"] == "PASS")
    failed = total - passed
    warns  = sum(1 for r in all_results if r.get("status") == "WARN")
    errors = sum(1 for r in all_results if r.get("status") == "ERROR")

    pct = round((passed / total * 100) if total > 0 else 0, 1)

    grade_thresholds = [(90, "A"), (75, "B"), (60, "C"), (40, "D")]
    grade = next((g for threshold, g in grade_thresholds if pct >= threshold), "F")

    return {
        "overall_compliance_pct": pct,
        "total_checks": total,
        "passed": passed,
        "failed": failed,
        "warnings": warns,
        "errors": errors,
        "grade": grade,
        "status": "COMPLIANT" if pct >= 75 else "NON_COMPLIANT",
    }

# =========================================================
# REPORT GENERATION - DOCX ONLY
# =========================================================

def check_node_dependencies() -> tuple:
    """Verify Node.js and docx package are available."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return False, "Node.js not found. Install from https://nodejs.org"
        node_version = result.stdout.strip()
    except FileNotFoundError:
        return False, "Node.js not found in PATH. Install from https://nodejs.org"

    check_script = "try{require('docx');console.log('OK')}catch(e){console.error(e.message)}"
    result = subprocess.run(
        ["node", "-e", check_script],
        capture_output=True, text=True, timeout=10
    )
    if "OK" not in result.stdout:
        return False, (
            f"Node.js {node_version} found, but 'docx' package missing.\n"
            f"Fix: npm install docx\n"
            f"Error: {result.stderr.strip()}"
        )

    return True, f"Node.js {node_version} with docx package: OK"


def save_docx_report(all_results: list, summary: dict, url: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_json = REPORTS_DIR / "_audit_data.json"
    tmp_json.write_text(
        json.dumps({"url": url, "summary": summary, "results": all_results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    final_path = (REPORTS_DIR / "report.docx").resolve()
    # Write to a temporary file first to avoid EBUSY when report.docx is open in Word
    tmp_out_path = (REPORTS_DIR / f"_report_tmp_{int(time.time())}.docx").resolve()

    node_script = r"""
"use strict";
const fs   = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle,
  WidthType, ShadingType, VerticalAlign, PageNumber, PageBreak,
  ImageRun, LevelFormat, PageOrientation,
} = require("docx");

const DATA_PATH   = process.argv[2];
const OUTPUT_PATH = process.argv[3];

const { url, summary, results } = JSON.parse(
  fs.readFileSync(DATA_PATH, "utf-8")
);

const NAVY   = "162F6A";
const WHITE  = "FFFFFF";
const GREEN  = "198754";
const RED    = "DC3545";
const AMBER  = "DDA73A";
const LGRAY  = "F2F2F2";
const MGRAY  = "CCCCCC";
const DKGRAY = "555555";

const PORTRAIT_W  = 12240;
const PORTRAIT_H  = 15840;
const MARGIN      = 1080;
const PORTRAIT_CW = PORTRAIT_W - MARGIN * 2;

const LANDSCAPE_W  = 15840;
const LANDSCAPE_H  = 12240;
const LANDSCAPE_CW = LANDSCAPE_W - MARGIN * 2;

function cell(text, widthDxa, opts = {}) {
  const {
    bold = false, color = "000000", bg = null,
    fontSize = 18, align = AlignmentType.LEFT,
  } = opts;
  return new TableCell({
    width: { size: widthDxa, type: WidthType.DXA },
    shading: bg ? { fill: bg, type: ShadingType.CLEAR } : undefined,
    margins: { top: 40, bottom: 40, left: 60, right: 60 },
    verticalAlign: VerticalAlign.CENTER,
    borders: {
      top:    { style: BorderStyle.SINGLE, size: 1, color: MGRAY },
      bottom: { style: BorderStyle.SINGLE, size: 1, color: MGRAY },
      left:   { style: BorderStyle.SINGLE, size: 1, color: MGRAY },
      right:  { style: BorderStyle.SINGLE, size: 1, color: MGRAY },
    },
    children: [new Paragraph({
      alignment: align,
      children: [new TextRun({ text: String(text), bold, color, size: fontSize, font: "Arial" })],
    })],
  });
}

function hdrCell(text, widthDxa) {
  return cell(text, widthDxa, { bold: true, color: WHITE, bg: NAVY, fontSize: 18 });
}

function spacer(pts = 80) {
  return new Paragraph({ spacing: { before: pts, after: pts }, children: [] });
}

function heading(text, level = HeadingLevel.HEADING_1) {
  return new Paragraph({
    heading: level,
    children: [new TextRun({ text, font: "Arial" })],
  });
}

function para(text, opts = {}) {
  const { bold = false, color = "000000", size = 20 } = opts;
  return new Paragraph({
    children: [new TextRun({ text, bold, color, size, font: "Arial" })],
  });
}

const GRADE_COLOR = { A: GREEN, B: "5279D7", C: AMBER, D: "916100", F: RED };
const gradeColor = GRADE_COLOR[summary.grade] || DKGRAY;

function summaryTable() {
  const cols  = [1600, 1600, 1600, 1600, 1600, 1600];
  const labels = ["Grade", "Compliance %", "Total Checks", "Passed", "Failed", "Status"];
  const values = [
    summary.grade,
    summary.overall_compliance_pct + "%",
    String(summary.total_checks),
    String(summary.passed),
    String(summary.failed),
    summary.status,
  ];
  const colors = [gradeColor, gradeColor, "000000", GREEN, RED, gradeColor];

  return new Table({
    width: { size: 9600, type: WidthType.DXA },
    columnWidths: cols,
    rows: [
      new TableRow({ children: labels.map((l, i) => hdrCell(l, cols[i])) }),
      new TableRow({
        children: values.map((v, i) =>
          cell(v, cols[i], { bold: true, color: colors[i], fontSize: 22, align: AlignmentType.CENTER })
        ),
      }),
    ],
  });
}

function reqSummaryTable() {
  const byReq = {};
  results.forEach(r => {
    if (!byReq[r.requirement]) byReq[r.requirement] = { name: r.name, pass: 0, fail: 0 };
    r.status === "PASS" ? byReq[r.requirement].pass++ : byReq[r.requirement].fail++;
  });

  const cols = [1200, 3600, 1200, 1200, 2400];
  const rows = [
    new TableRow({
      children: [
        hdrCell("Req.", cols[0]), hdrCell("Name", cols[1]),
        hdrCell("Pass", cols[2]), hdrCell("Fail", cols[3]),
        hdrCell("Result", cols[4]),
      ],
    }),
    ...Object.entries(byReq).sort().map(([req, d]) => {
      const overall = d.fail === 0 ? "PASS" : "FAIL";
      const bgResult = d.fail === 0 ? "D4EDDA" : "F8D7DA";
      return new TableRow({
        children: [
          cell(req,       cols[0], { bold: true }),
          cell(d.name,    cols[1]),
          cell(d.pass,    cols[2], { color: GREEN, align: AlignmentType.CENTER }),
          cell(d.fail,    cols[3], { color: d.fail > 0 ? RED : GREEN, align: AlignmentType.CENTER }),
          cell(overall,   cols[4], { bold: true, color: d.fail === 0 ? GREEN : RED,
                                     bg: bgResult, align: AlignmentType.CENTER }),
        ],
      });
    }),
  ];

  return new Table({ width: { size: 9600, type: WidthType.DXA }, columnWidths: cols, rows });
}

function detailTable() {
  const cols = [1000, 2200, 900, 4200, 2300, 2600];

  const headerRow = new TableRow({
    tableHeader: true,
    children: [
      hdrCell("Req.",     cols[0]),
      hdrCell("Name",     cols[1]),
      hdrCell("Status",   cols[2]),
      hdrCell("Reason",   cols[3]),
      hdrCell("Actual",   cols[4]),
      hdrCell("Expected", cols[5]),
    ],
  });

  const dataRows = results.map(r => {
    const isPass = r.status === "PASS";
    return new TableRow({
      children: [
        cell(r.requirement, cols[0], { bold: true }),
        cell(r.name,        cols[1]),
        cell(r.status,      cols[2], {
          bold: true,
          color: isPass ? GREEN : RED,
          bg: isPass ? "D4EDDA" : "F8D7DA",
          align: AlignmentType.CENTER,
        }),
        cell(r.reason,    cols[3], { fontSize: 16 }),
        cell(r.actual,    cols[4], { fontSize: 16 }),
        cell(r.expected,  cols[5], { fontSize: 16 }),
      ],
    });
  });

  return new Table({
    width: { size: 13200, type: WidthType.DXA },
    columnWidths: cols,
    rows: [headerRow, ...dataRows],
  });
}

function screenshotSection() {
  const children = [
    heading("Evidence Screenshots", HeadingLevel.HEADING_2),
    spacer(40),
  ];

  const evidenceByReq = {};
  results.forEach(r => {
    if (r.evidence && fs.existsSync(r.evidence)) {
      if (!evidenceByReq[r.requirement]) {
        evidenceByReq[r.requirement] = [];
      }
      evidenceByReq[r.requirement].push(r);
    }
  });

  const totalScreenshots = Object.values(evidenceByReq).reduce((sum, arr) => sum + arr.length, 0);
  
  if (totalScreenshots === 0) {
    children.push(para("No evidence screenshots available.", { color: DKGRAY }));
    return children;
  }

  Object.keys(evidenceByReq).sort().forEach(reqId => {
    const items = evidenceByReq[reqId];
    
    items.slice(0, 10).forEach(r => {
      try {
        const imgBuf  = fs.readFileSync(r.evidence);
        const isPass  = r.status === "PASS";

        children.push(new Paragraph({
          children: [
            new TextRun({ text: `${r.requirement} - ${r.name}  `, bold: true, font: "Arial", size: 20 }),
            new TextRun({
              text: r.status,
              bold: true,
              color: isPass ? GREEN : RED,
              font: "Arial",
              size: 20,
            }),
          ],
        }));
        children.push(para(r.reason.substring(0, 200), { color: DKGRAY, size: 18 }));
        children.push(spacer(20));

        children.push(new Paragraph({
          children: [
            new ImageRun({
              type: "png",
              data: imgBuf,
              transformation: { width: 600, height: 338 },
              altText: { title: r.requirement, description: r.reason.substring(0, 100), name: r.requirement },
            }),
          ],
        }));
        children.push(spacer(60));
      } catch (_) {}
    });
  });

  return children;
}

function makeHeader(contentW) {
  return new Header({
    children: [new Paragraph({
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: NAVY, space: 1 } },
      children: [
        new TextRun({ text: "DBIM F-1 Compliance Report", bold: true, color: NAVY, font: "Arial", size: 20 }),
        new TextRun({ text: "\t" + new Date().toLocaleDateString("en-IN"), font: "Arial", size: 18, color: DKGRAY }),
      ],
      tabStops: [{ type: "right", position: contentW }],
    })],
  });
}

function makeFooter(contentW) {
  return new Footer({
    children: [new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 4, color: MGRAY, space: 1 } },
      children: [
        new TextRun({ text: "Page ", font: "Arial", size: 16, color: DKGRAY }),
        new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: DKGRAY }),
        new TextRun({ text: " of ", font: "Arial", size: 16, color: DKGRAY }),
        new TextRun({ children: [PageNumber.TOTAL_PAGES], font: "Arial", size: 16, color: DKGRAY }),
        new TextRun({ text: "\tDBIM F-1 Automation Engine", font: "Arial", size: 16, color: DKGRAY }),
      ],
      tabStops: [{ type: "right", position: contentW }],
    })],
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 20 } } },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run:  { size: 36, bold: true, color: NAVY, font: "Arial" },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 0 },
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run:  { size: 28, bold: true, color: NAVY, font: "Arial" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 1 },
      },
    ],
  },
  numbering: { config: [] },
  sections: [
    {
      properties: {
        page: {
          size: { width: PORTRAIT_W, height: PORTRAIT_H, orientation: PageOrientation.PORTRAIT },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
        },
      },
      headers: { default: makeHeader(PORTRAIT_CW) },
      footers: { default: makeFooter(PORTRAIT_CW) },
      children: [
        spacer(200),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "DBIM F-1 Fundamental Requirements", bold: true, size: 56, color: NAVY, font: "Arial" })],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "Compliance Audit Report", bold: true, size: 40, color: NAVY, font: "Arial" })],
        }),
        spacer(60),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: NAVY, space: 4 } },
          children: [new TextRun({ text: url, size: 22, color: "5279D7", font: "Arial" })],
        }),
        spacer(60),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({
            text: "Generated: " + new Date().toLocaleString("en-IN"),
            size: 18, color: DKGRAY, font: "Arial",
          })],
        }),
        spacer(120),

        new Paragraph({ children: [new PageBreak()] }),
        heading("1. Executive Summary"),
        spacer(40),
        summaryTable(),
        spacer(80),
        para(
          `Overall compliance score: ${summary.overall_compliance_pct}%  ` +
          `(${summary.passed} of ${summary.total_checks} checks passed).  ` +
          `Final grade: ${summary.grade}  |  Status: ${summary.status}` +
          (summary.warnings > 0 ? `  |  Warnings: ${summary.warnings}` : "") +
          (summary.errors > 0 ? `  |  Errors: ${summary.errors}` : ""),
          { size: 20 }
        ),
        spacer(40),
        para("Grade Scale: A = 90-100  |  B = 75-89  |  C = 60-74  |  D = 40-59  |  F = 0-39",
             { color: DKGRAY, size: 18 }),

        new Paragraph({ children: [new PageBreak()] }),
        heading("2. Requirement-Wise Summary"),
        spacer(40),
        reqSummaryTable(),
      ],
    },
    {
      properties: {
        page: {
          size: { width: LANDSCAPE_W, height: LANDSCAPE_H, orientation: PageOrientation.LANDSCAPE },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
        },
      },
      headers: { default: makeHeader(LANDSCAPE_CW) },
      footers: { default: makeFooter(LANDSCAPE_CW) },
      children: [
        heading("3. Detailed Findings"),
        spacer(40),
        detailTable(),

        new Paragraph({ children: [new PageBreak()] }),
        ...screenshotSection(),
      ],
    },
  ],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUTPUT_PATH, buf);
  console.log("OK:" + OUTPUT_PATH);
}).catch(err => {
  console.error("ERROR:" + err.message);
  process.exit(1);
});
"""

    tmp_js = REPORTS_DIR / "_docx_gen.js"
    tmp_js.write_text(node_script, encoding="utf-8")

    node_cwd = REPORTS_DIR.resolve()
    candidate = node_cwd
    for _ in range(6):
        if (candidate / "node_modules" / "docx").exists():
            node_cwd = candidate
            break
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    else:
        node_cwd = Path.cwd()

    actual_output_path = final_path
    try:
        # Step 1: Generate DOCX to a temporary path (always succeeds)
        result = subprocess.run(
            ["node", str(tmp_js), str(tmp_json.resolve()), str(tmp_out_path)],
            capture_output=True, text=True, timeout=120,
            cwd=str(node_cwd),
        )
        if result.returncode != 0 or "ERROR:" in result.stdout:
            err_msg = result.stderr or result.stdout
            raise RuntimeError(f"Node.js docx generation failed:\n{err_msg}")

        # Step 2: Try to atomically replace report.docx with the new file
        try:
            os.replace(str(tmp_out_path), str(final_path))
            actual_output_path = final_path
            print(f"  DOCX report: {final_path}")
        except (PermissionError, OSError) as lock_err:
            # report.docx is locked (likely open in Word) — save to timestamped fallback
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            fallback_path = (REPORTS_DIR / f"report_{timestamp}.docx").resolve()
            try:
                os.replace(str(tmp_out_path), str(fallback_path))
                actual_output_path = fallback_path
                print(f"  ⚠️  '{final_path.name}' is currently open/locked.")
                print(f"  DOCX report saved as: {fallback_path}")
                print(f"  Close MS Word and re-run to overwrite '{final_path.name}'.")
            except Exception as fb_err:
                raise RuntimeError(
                    f"Could not write DOCX. '{final_path.name}' is locked "
                    f"(close MS Word) and fallback also failed: {fb_err}"
                )

    finally:
        try: tmp_js.unlink(missing_ok=True)
        except Exception: pass
        try: tmp_json.unlink(missing_ok=True)
        except Exception: pass
        try:
            if tmp_out_path.exists():
                tmp_out_path.unlink(missing_ok=True)
        except Exception: pass

    return actual_output_path

# =========================================================
# URL VALIDATION
# =========================================================

def prepare_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {raw}")
    
    safe, reason = is_safe_url(raw)
    if not safe:
        raise ValueError(f"URL rejected for security reasons: {reason}")
    
    return raw


def validate_url(url: str) -> bool:
    try:
        resp = requests.head(url, timeout=15, allow_redirects=True, verify=False)
        return resp.status_code < 500
    except requests.exceptions.SSLError:
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True, verify=False)
            return True
        except Exception:
            return False
    except Exception:
        return False

# =========================================================
# WAF BYPASS & BLOCK DETECTION
# =========================================================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};
delete navigator.__playwright__;
delete window.__playwright__;
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});
"""

def random_user_agent():
    return random.choice(USER_AGENTS)

async def create_stealth_context(browser):
    context = await browser.new_context(
        user_agent=random_user_agent(),
        extra_http_headers=EXTRA_HEADERS,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=True,
    )
    await context.add_init_script(STEALTH_SCRIPT)
    return context

BLOCK_KEYWORDS = [
    "access denied", "blocked", "security check", "captcha", "robot check",
    "you have been blocked", "forbidden", "not authorized", "rate limit",
    "too many requests", "please verify you are human"
]

async def detect_block(page, response_status=None, final_url=None):
    if response_status and response_status in (403, 429, 503):
        return True, f"HTTP {response_status} - Blocked"

    if final_url:
        url_lower = final_url.lower()
        if any(x in url_lower for x in ("captcha", "denied", "blocked", "error")):
            return True, f"Redirected to suspicious URL: {final_url}"

    try:
        content = await page.content()
        content_lower = content.lower()
        for kw in BLOCK_KEYWORDS:
            if kw in content_lower:
                snippet = content[max(0, content.find(kw)-80):content.find(kw)+80].replace('\n', ' ')
                return True, f"Page contains block keyword '{kw}'. Snippet: {snippet[:150]}"
    except Exception:
        pass

    return False, None

# =========================================================
# MAIN ENGINE
# =========================================================

async def run_engine(url: str):
    print(f"\n{'='*65}")
    print(f"  DBIM F-1 Compliance Engine")
    print(f"  Target: {url}")
    print(f"{'='*65}\n")

    print("[1/5] Pre-flight checks...")
    node_ok, node_msg = check_node_dependencies()
    if not node_ok:
        print(f"  WARNING: {node_msg}")
        print("  DOCX report generation may fail. Continuing...")
    else:
        print(f"  {node_msg}")
    
    print("[2/5] Launching browser with anti-detection measures...")
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-setuid-sandbox", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
            ],
        )
        
        try:
            context = await create_stealth_context(browser)
            page = await context.new_page()

            response = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"  Initial load error: {e}")

            await page.wait_for_timeout(5000)
            final_url = page.url
            status = response.status if response else None

            blocked, block_reason = await detect_block(page, status, final_url)
            if blocked:
                print(f"\n⚠️  WARNING: Website is blocking automated access!")
                print(f"   Reason: {block_reason}")
                print(f"   URL: {final_url}")
                print(f"   Cannot perform compliance audit.\n")
                (SCREENSHOTS_DIR / "F1_00").mkdir(parents=True, exist_ok=True)
                screenshot_path = SCREENSHOTS_DIR / "F1_00" / "access_denied.png"
                await page.screenshot(path=str(screenshot_path))
                print(f"   Screenshot saved: {screenshot_path}")
                return {
                    "summary": {"status": "BLOCKED", "reason": block_reason},
                    "results": []
                }

            print("[3/5] Extracting rendered styles and colours...")

            try:
                await page.evaluate(
                    """
                    async () => {
                        for (let i = 0; i < 6; i++) {
                            window.scrollBy(0, window.innerHeight);
                            await new Promise(r => setTimeout(r, 300));
                        }
                        window.scrollTo(0, 0);
                    }
                    """
                )
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  Scroll interrupted (page navigated): {e}")
                await page.wait_for_timeout(3000)

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            try:
                elements = await extract_rendered_styles(page)
                colours = extract_colours_from_elements(elements)
            except Exception as e:
                print(f"  Style extraction failed: {e}")
                elements = []
                colours = []

            print("[4/5] Detecting dominant colour group...")
            dominant_result = detect_dominant_group(colours)
            print(f"  Dominant group: {dominant_result['dominant_group']} (confidence: {dominant_result['confidence']:.1%})")

            print("[5/5] Running F1-01 through F1-10 checks...")

            print("  F1-01: Primary Colour Palette")
            r01 = await check_f1_01(page, url, colours, dominant_result)
            all_results.append(r01)

            print("  F1-02: Functional Colour Palette")
            r02 = await check_f1_02(page, url, colours)
            all_results.extend(r02)

            print("  F1-03: Icon Colours")
            r03 = await check_f1_03(page, url, dominant_result)
            all_results.extend(r03)

            print("  F1-04: Footer Background Colour")
            r04 = await check_f1_04(page, url, dominant_result)
            all_results.append(r04)

            print("  F1-05: Consistent Icon Style")
            r05 = await check_f1_05(page, url)
            all_results.append(r05)

            print("  F1-06: DBIM Icon Set Usage")
            r06 = await check_f1_06(page, url)
            all_results.append(r06)

            print("  F1-07: Icon File Format")
            r07 = await check_f1_07(page, url)
            all_results.extend(r07)

            print("  F1-08: Icon Size")
            r08 = await check_f1_08(page, url)
            all_results.extend(r08)

            print("  F1-09: Icon Aspect Ratio")
            r09 = await check_f1_09(page, url)
            all_results.extend(r09)

            print("  F1-10: Icon Contrast on Images")
            r10 = await check_f1_10(page, url)
            all_results.extend(r10)

        finally:
            await browser.close()

    print("[6/5] Generating DOCX report...")
    summary = score_results(all_results)
    try:
        save_docx_report(all_results, summary, url)
    except Exception as e:
        print(f"  WARNING: DOCX report generation failed: {e}")
        print(f"  JSON audit data saved to reports/ directory for manual inspection.")

    print(f"\n{'='*65}")
    print(f"  COMPLIANCE SUMMARY")
    print(f"{'='*65}")
    print(f"  Grade:          {summary['grade']}")
    print(f"  Compliance:     {summary['overall_compliance_pct']}%")
    print(f"  Total Checks:   {summary['total_checks']}")
    print(f"  Passed:         {summary['passed']}")
    print(f"  Failed:         {summary['failed']}")
    print(f"  Warnings:       {summary['warnings']}")
    print(f"  Errors:         {summary['errors']}")
    print(f"  Status:         {summary['status']}")
    print(f"{'='*65}")
    print(f"  Report saved to: {(REPORTS_DIR / 'report.docx').resolve()}")
    print(f"  Screenshots in:  {SCREENSHOTS_DIR.resolve()}")
    print(f"{'='*65}\n")

    return {"summary": summary, "results": all_results}

# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if len(sys.argv) > 1:
        raw_url = sys.argv[1]
    else:
        raw_url = input("Enter website URL: ").strip()

    try:
        url = prepare_url(raw_url)
    except ValueError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)

    print(f"\nValidating: {url}")
    if not validate_url(url):
        print(f"WARNING: Could not reach {url}. Proceeding anyway (site may still load via browser)...")

    try:
        asyncio.run(run_engine(url))
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)