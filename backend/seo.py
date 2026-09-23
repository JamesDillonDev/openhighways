"""Crawlable pages for the map: one per camera, road and region, plus the
sitemap that lists them.

The map itself is a single-page app, so without this every URL is the same
empty <div id="root"> - one page for a search engine to rank, with nothing on
it. Each page here is the built index.html with its own title, description,
canonical and structured data swapped into the head, and a plain-HTML
summary (with links on to related pages) inside #root. The React app replaces
that summary when it mounts and opens the same camera/road/region itself
(frontend/src/routes.js holds the matching client-side rules - keep the two
in step).
"""

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from markupsafe import escape

import db
from config import section
from models import CameraRecord

# Canonical URLs always name the public domain, whichever host served the
# request - otherwise openhighway.fly.dev and openhighways.uk would each
# claim to be the original of the same page.
SITE_URL = os.environ.get("SITE_URL", section("api").get("site_url", "https://openhighways.uk")).rstrip("/")

SITE_NAME = "OpenHighways"
OG_IMAGE = f"{SITE_URL}/og-image.png"

SOURCE_LABELS = {
    "national_highways": "National Highways",
    "tfl": "Transport for London",
    "traffic_scotland": "Traffic Scotland",
    "traffic_wales": "Traffic Wales",
    "northern_ireland": "TrafficWatchNI",
}

# URL slug -> (label, source). One provider per region, in the map filter's
# display order.
REGIONS = {
    "england": ("England", "national_highways"),
    "london": ("London", "tfl"),
    "scotland": ("Scotland", "traffic_scotland"),
    "wales": ("Wales", "traffic_wales"),
    "northern-ireland": ("Northern Ireland", "northern_ireland"),
}

SOURCE_REGION = {source: slug for slug, (_, source) in REGIONS.items()}

# Great Britain shares one road numbering (the A1 runs from London into
# Scotland), but Northern Ireland has its own - its A1 is a different road,
# so its roads get their own URLs.
NI_SOURCES = {"northern_ireland"}

# The camera list only changes when a source sync runs; a crawler walking
# thousands of pages shouldn't re-read the whole table for each one.
CAMERA_CACHE_SECONDS = 60

_camera_cache: tuple[float, list[CameraRecord]] = (0.0, [])
_camera_cache_lock = threading.Lock()

_template: Optional[str] = None

HEAD_BLOCK = re.compile(r"<!--seo-head-->.*?<!--/seo-head-->", re.S)
ROOT_DIV = '<div id="root"></div>'


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def network(source: str) -> str:
    return "ni" if source in NI_SOURCES else "gb"


def camera_path(camera: CameraRecord) -> str:
    return f"/camera/{camera.master_id}"


def road_path(net: str, road: str) -> str:
    return f"/road/ni/{slugify(road)}" if net == "ni" else f"/road/{slugify(road)}"


def region_path(slug: str) -> str:
    return f"/region/{slug}"


def _cameras() -> list[CameraRecord]:
    """Every active camera the map can place, briefly cached."""

    global _camera_cache

    with _camera_cache_lock:

        fetched_at, cameras = _camera_cache

        if time.monotonic() - fetched_at > CAMERA_CACHE_SECONDS:

            conn = db.get_connection()

            try:
                records = db.list_cameras(conn, active_only=True)
            finally:
                conn.close()

            cameras = [
                record for record in records
                if record.latitude is not None and record.longitude is not None
            ]
            _camera_cache = (time.monotonic(), cameras)

    return cameras


def _road_sort_key(road: str):
    """M roads, then A roads, then the rest - each in numeric order."""

    match = re.match(r"([A-Za-z]+)(\d+)(.*)", road)

    if not match:
        return (3, road, 0, "")

    prefix, number, rest = match.groups()

    return ({"M": 0, "A": 1}.get(prefix.upper(), 2), prefix.upper(), int(number), rest)


def _roads(cameras: list[CameraRecord]) -> dict[tuple[str, str], list[CameraRecord]]:
    """(network, road slug) -> that road's cameras, in road order."""

    roads: dict[tuple[str, str], list[CameraRecord]] = {}

    for camera in cameras:
        if camera.road and slugify(camera.road):
            roads.setdefault((network(camera.source), slugify(camera.road)), []).append(camera)

    return dict(sorted(roads.items(), key=lambda item: (item[0][0], _road_sort_key(item[1][0].road))))


def _camera_name(camera: CameraRecord) -> str:
    return camera.name or f"Camera {camera.master_id}"


def _by_name(cameras: list[CameraRecord]) -> list[CameraRecord]:
    """Numbers compared as numbers, so "M25 9/1A" comes before "M25 10/1A"."""

    def key(camera: CameraRecord):
        parts = re.split(r"(\d+)", _camera_name(camera).lower())
        return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts]

    return sorted(cameras, key=key)


def _places_text(cameras: list[CameraRecord]) -> str:
    """Where a set of cameras is, in words - "England and Wales"."""

    sources = {camera.source for camera in cameras}

    if sources == {"tfl"}:
        return "London"

    countries = []

    for slug, (label, source) in REGIONS.items():
        country = "England" if slug == "london" else label
        if source in sources and country not in countries:
            countries.append(country)

    if len(countries) <= 1:
        return "".join(countries)

    return ", ".join(countries[:-1]) + " and " + countries[-1]


def _template_html(frontend_dist: Path) -> str:

    global _template

    if _template is None:
        _template = (frontend_dist / "index.html").read_text(encoding="utf-8")

    return _template


def _json_ld(data: dict) -> str:
    # "</" inside a string would close the <script> early.
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _breadcrumbs(items: list[tuple[str, str]]) -> dict:
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": f"{SITE_URL}{path}"}
            for i, (name, path) in enumerate(items, start=1)
        ],
    }


def _head(title: str, description: str, path: Optional[str], graph: list[dict], noindex: bool = False) -> str:

    title, description = escape(title), escape(description)

    tags = [
        f"<title>{title}</title>",
        f'<meta name="description" content="{description}" />',
        f'<meta name="robots" content="{"noindex" if noindex else "index, follow"}" />',
    ]

    if path is not None:
        url = escape(f"{SITE_URL}{path}")
        tags += [
            f'<link rel="canonical" href="{url}" />',
            f'<meta property="og:url" content="{url}" />',
        ]

    tags += [
        f'<meta property="og:title" content="{title}" />',
        f'<meta property="og:description" content="{description}" />',
        '<meta property="og:type" content="website" />',
        f'<meta property="og:image" content="{OG_IMAGE}" />',
        f'<meta property="og:site_name" content="{SITE_NAME}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{title}" />',
        f'<meta name="twitter:description" content="{description}" />',
        f'<meta name="twitter:image" content="{OG_IMAGE}" />',
    ]

    if graph:
        data = {"@context": "https://schema.org", "@graph": graph}
        tags.append(f'<script type="application/ld+json">{_json_ld(data)}</script>')

    return "\n    ".join(tags)


def _render(frontend_dist: Path, body: str, head: Optional[str] = None) -> str:
    """The built index.html with this page's head tags and #root content -
    the head is left as built (the home page's) when none is given."""

    html = _template_html(frontend_dist)

    if head is not None:
        html = HEAD_BLOCK.sub(lambda _: head, html, count=1)

    return html.replace(ROOT_DIV, f'<div id="root">{body}</div>', 1)


def _link(path: str, text: str) -> str:
    return f'<a href="{escape(path)}">{escape(text)}</a>'


def _link_list(links: list[tuple[str, str]]) -> str:
    items = "".join(f"<li>{_link(path, text)}</li>" for path, text in links)
    return f"<ul>{items}</ul>"


def _nearby(camera: CameraRecord, cameras: list[CameraRecord], count: int = 6) -> list[CameraRecord]:

    scale = math.cos(math.radians(camera.latitude))

    def distance(other: CameraRecord) -> float:
        return (other.latitude - camera.latitude) ** 2 + ((other.longitude - camera.longitude) * scale) ** 2

    others = [other for other in cameras if other.master_id != camera.master_id]

    return sorted(others, key=distance)[:count]


def home_page(frontend_dist: Path) -> str:

    cameras = _cameras()
    region_counts = {source: 0 for _, source in REGIONS.values()}

    for camera in cameras:
        if camera.source in region_counts:
            region_counts[camera.source] += 1

    regions = [
        (region_path(slug), f"{label} traffic cameras ({region_counts[source]})")
        for slug, (label, source) in REGIONS.items()
        if region_counts[source]
    ]

    roads = [
        (road_path(net, road_cameras[0].road), f"{road_cameras[0].road}{' (Northern Ireland)' if net == 'ni' else ''}")
        for (net, _), road_cameras in _roads(cameras).items()
    ]

    body = (
        "<main>"
        "<h1>UK Traffic Cameras</h1>"
        f"<p>A free live map of {len(cameras)} traffic cameras across the UK, from "
        "National Highways, Transport for London, Traffic Scotland, Traffic Wales and TrafficWatchNI.</p>"
        f"<h2>Cameras by region</h2>{_link_list(regions)}"
        f"<h2>Cameras by road</h2>{_link_list(roads)}"
        "</main>"
    )

    return _render(frontend_dist, body)


def camera_page(frontend_dist: Path, master_id: int) -> Optional[str]:

    cameras = _cameras()
    camera = next((camera for camera in cameras if camera.master_id == master_id), None)

    if camera is None:
        return None

    name = _camera_name(camera)
    region_slug = SOURCE_REGION.get(camera.source)
    region_label = REGIONS[region_slug][0] if region_slug else ""
    provider = SOURCE_LABELS.get(camera.source, camera.source)
    net = network(camera.source)

    # "M25 J10 traffic camera - M25" says the road twice.
    title_road = f" - {camera.road}" if camera.road and camera.road.lower() not in name.lower() else ""
    title = f"{name} traffic camera{title_road} | {SITE_NAME}"

    on_road = f" on the {camera.road}" if camera.road else ""
    in_region = f" in {region_label}" if region_label else ""
    description = (
        f"Live traffic camera at {name}{on_road}{in_region}, from {provider}. "
        "See the latest image, the current vehicle count and recent traffic levels."
    )

    crumbs = [(SITE_NAME, "/")]
    if region_slug:
        crumbs.append((f"{region_label} traffic cameras", region_path(region_slug)))
    if camera.road:
        crumbs.append((f"{camera.road} traffic cameras", road_path(net, camera.road)))
    crumbs.append((name, camera_path(camera)))

    place = {
        "@type": "Place",
        "name": f"{name} traffic camera",
        "url": f"{SITE_URL}{camera_path(camera)}",
        "geo": {"@type": "GeoCoordinates", "latitude": camera.latitude, "longitude": camera.longitude},
    }

    details = [("Road", camera.road), ("Direction", camera.direction), ("Region", region_label), ("Provider", provider)]
    details_html = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in details if value
    )

    links = []
    if camera.road:
        links.append((road_path(net, camera.road), f"All {camera.road} traffic cameras"))
    if region_slug:
        links.append((region_path(region_slug), f"All {region_label} traffic cameras"))

    nearby = [(camera_path(other), _camera_name(other)) for other in _nearby(camera, cameras)]

    body = (
        "<main>"
        f"<h1>{escape(name)} traffic camera</h1>"
        f"<p>{escape(description)}</p>"
        f"<dl>{details_html}</dl>"
        f"{_link_list(links)}"
        f"<h2>Nearby cameras</h2>{_link_list(nearby)}"
        "</main>"
    )

    head = _head(title, description, camera_path(camera), [place, _breadcrumbs(crumbs)])

    return _render(frontend_dist, body, head)


def road_page(frontend_dist: Path, net: str, road_slug: str) -> Optional[str]:

    road_cameras = _roads(_cameras()).get((net, road_slug))

    if not road_cameras:
        return None

    road = road_cameras[0].road
    places = _places_text(road_cameras)
    path = road_path(net, road)
    count = len(road_cameras)
    noun = "camera" if count == 1 else "cameras"

    title = f"{road} traffic cameras{', Northern Ireland' if net == 'ni' else ''} - live images | {SITE_NAME}"
    description = (
        f"{count} live traffic {noun} on the {road} in {places}. "
        "Check current road conditions and traffic levels at each camera."
    )

    links = [(camera_path(camera), _camera_name(camera)) for camera in _by_name(road_cameras)]

    body = (
        "<main>"
        f"<h1>{escape(road)} traffic cameras</h1>"
        f"<p>{escape(description)}</p>"
        f"{_link_list(links)}"
        "</main>"
    )

    head = _head(title, description, path, [_breadcrumbs([(SITE_NAME, "/"), (f"{road} traffic cameras", path)])])

    return _render(frontend_dist, body, head)


def region_page(frontend_dist: Path, region_slug: str) -> Optional[str]:

    if region_slug not in REGIONS:
        return None

    label, source = REGIONS[region_slug]
    region_cameras = [camera for camera in _cameras() if camera.source == source]

    if not region_cameras:
        return None

    path = region_path(region_slug)
    provider = SOURCE_LABELS.get(source, source)

    title = f"{label} traffic cameras - live road camera map | {SITE_NAME}"
    description = (
        f"{len(region_cameras)} live traffic cameras across {label} from {provider}. "
        "Browse them by road on a live map, with vehicle counts for each camera."
    )

    roads = _roads(region_cameras)
    road_links = [
        (road_path(net, cameras[0].road), f"{cameras[0].road} ({len(cameras)})")
        for (net, _), cameras in roads.items()
    ]

    # Cameras with no known road are only reachable from here.
    unroaded = [(camera_path(camera), _camera_name(camera)) for camera in _by_name(region_cameras) if not camera.road]

    body = (
        "<main>"
        f"<h1>{escape(label)} traffic cameras</h1>"
        f"<p>{escape(description)}</p>"
        + (f"<h2>Cameras by road</h2>{_link_list(road_links)}" if road_links else "")
        + (f"<h2>Other cameras</h2>{_link_list(unroaded)}" if unroaded else "")
        + "</main>"
    )

    head = _head(title, description, path, [_breadcrumbs([(SITE_NAME, "/"), (f"{label} traffic cameras", path)])])

    return _render(frontend_dist, body, head)


def not_found_page(frontend_dist: Path) -> str:

    body = f"<main><h1>Page not found</h1><p>{_link('/', 'Back to the traffic camera map')}</p></main>"
    head = _head(f"Page not found | {SITE_NAME}", "This page doesn't exist.", None, [], noindex=True)

    return _render(frontend_dist, body, head)


def sitemap_xml() -> str:

    cameras = _cameras()
    sources = {camera.source for camera in cameras}

    paths = ["/"]
    paths += [region_path(slug) for slug, (_, source) in REGIONS.items() if source in sources]
    paths += [road_path(net, road_cameras[0].road) for (net, _), road_cameras in _roads(cameras).items()]
    paths += [camera_path(camera) for camera in cameras]

    urls = "".join(f"<url><loc>{xml_escape(SITE_URL + path)}</loc></url>" for path in paths)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n'
    )
