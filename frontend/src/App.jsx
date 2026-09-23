import { useEffect, useMemo, useState } from 'react'
import { createPortal, flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { MapContainer, TileLayer, Popup, ZoomControl, useMap } from 'react-leaflet'
import L from 'leaflet'
import { IoCameraOutline } from 'react-icons/io5'
import { locate } from 'leaflet.locatecontrol'
import 'leaflet/dist/leaflet.css'
import 'leaflet.locatecontrol/dist/L.Control.Locate.min.css'
import './App.css'

const UK_CENTER = [54.5, -3]
const POLL_INTERVAL_MS = 30000
const IMAGE_REFRESH_MS = 1000

const APP_VERSION = 'v1.5.3'
const REPO_URL = 'https://github.com/JamesDillonDev/openhighways'

// Friendlier labels for known sources - falls back to the raw name for any
// source the frontend doesn't recognise yet.
const SOURCE_LABELS = {
  national_highways: 'National Highways',
  tfl: 'Transport for London',
  traffic_scotland: 'Traffic Scotland',
  traffic_wales: 'Traffic Wales',
  northern_ireland: 'Traffic Watch NI',
}

// The map's filter list names regions rather than providers - a visitor
// cares where the cameras are, not who runs them (the camera panel and
// credits still name the provider). Listed in display order; any source
// missing here falls to the end under its provider label.
const REGION_LABELS = {
  national_highways: 'England',
  traffic_scotland: 'Scotland',
  tfl: 'London',
  traffic_wales: 'Wales',
  northern_ireland: 'Northern Ireland',
}

const REGION_ORDER = Object.keys(REGION_LABELS)

function regionRank(source) {
  const index = REGION_ORDER.indexOf(source)
  return index === -1 ? REGION_ORDER.length : index
}

// Each provider sets its own terms for reuse, and several specify the exact
// wording - TfL's three statements in particular are quoted verbatim from
// its Transport Data Service terms. Keep an entry here whenever a source is
// added, and take its wording from that source's own terms rather than
// paraphrasing it.
const SOURCE_CREDITS = [
  {
    source: 'national_highways',
    href: 'https://nationalhighways.co.uk/travel-updates/traffic-cameracctv-services/crown-copyright-notice/',
    lines: [
      'Images from National Highways\u2019 traffic management cameras.',
      '\u00a9 Crown copyright.',
    ],
  },
  {
    source: 'tfl',
    href: 'https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service',
    lines: [
      'Powered by TfL Open Data.',
      'Contains OS data \u00a9 Crown copyright and database rights 2016.',
      'Geomni UK Map data \u00a9 and database rights [2019].',
    ],
  },
  {
    source: 'traffic_scotland',
    href: 'https://www.traffic.gov.scot/',
    lines: ['Traffic camera images supplied by Traffic Scotland.'],
  },
  {
    source: 'traffic_wales',
    href: 'https://traffic.wales/developers',
    lines: ['Camera data sourced from Traffic Wales.'],
  },
  {
    source: 'northern_ireland',
    href: 'https://www.trafficwatchni.com/twni/crown-copyright',
    lines: [
      'Camera data from the DfI Traffic Information and Control Centre.',
      '\u00a9 Crown copyright, licensed under the Open Government Licence v3.0.',
    ],
  },
  {
    source: 'openstreetmap',
    href: 'https://www.openstreetmap.org/copyright',
    lines: [
      'Map tiles, and the road geometry used to place Welsh and Northern',
      'Irish cameras, \u00a9 OpenStreetMap contributors, licensed under the ODbL.',
    ],
  },
]

const CREDIT_LABELS = {
  ...SOURCE_LABELS,
  openstreetmap: 'OpenStreetMap',
}

// Colour-coded badge shown next to a camera's name so its source is
// identifiable at a glance - used as a fallback for any source without a
// logo below.
const SOURCE_COLORS = {
  national_highways: '#00549f',
  tfl: '#dc241f',
  traffic_scotland: '#0f7b43',
  traffic_wales: '#a3122a',
  northern_ireland: '#1d7a4c',
}

// Each provider's own logo, downloaded from their official site
// (frontend/public/logos) - shown in the camera preview panel only, not
// the source filter list (plain text reads better at that small size).
const SOURCE_LOGOS = {
  national_highways: '/logos/national_highways_full.png',
  tfl: '/logos/tfl_full.png',
  traffic_scotland: '/logos/traffic_scotland_full.png',
  traffic_wales: '/logos/traffic_wales_full.png',
  northern_ireland: '/logos/northern_ireland_full.png',
}

function SourceBadge({ source }) {
  const logo = SOURCE_LOGOS[source]
  const label = SOURCE_LABELS[source] || source
  const color = SOURCE_COLORS[source] || '#666'

  if (logo) {
    return <img className="source-badge source-badge-logo" src={logo} alt={label} title={label} />
  }

  return (
    <span className="source-badge" style={{ backgroundColor: color }}>
      {label}
    </span>
  )
}

// Wraps the leaflet.locatecontrol plugin so it renders under the zoom
// control with the same look and feel as OpenStreetMap/Leaflet's own UI.
function LocateControl() {
  const map = useMap()

  useEffect(() => {
    const control = locate({
      position: 'topright',
      flyTo: true,
      keepCurrentZoomLevel: false,
      initialZoomLevel: 14,
      strings: { title: 'Go to my location' },
    }).addTo(map)

    return () => control.remove()
  }, [map])

  return null
}

// Traffic-level colour scale: few/no vehicles reads as blue, heavy traffic
// as red. Anything at or above this count is treated as "full red".
const MAX_VEHICLES_FOR_COLOR = 12
const LOW_TRAFFIC_COLOR = [43, 108, 176] // blue
const HIGH_TRAFFIC_COLOR = [220, 38, 38] // red
const UNAVAILABLE_COLOR = '#a0a0a0'

function trafficColor(vehicles) {
  if (vehicles === undefined || vehicles === null) return UNAVAILABLE_COLOR

  const t = Math.min(vehicles / MAX_VEHICLES_FOR_COLOR, 1)

  const rgb = LOW_TRAFFIC_COLOR.map((from, i) =>
    Math.round(from + (HIGH_TRAFFIC_COLOR[i] - from) * t)
  )

  return `rgb(${rgb.join(',')})`
}

// Every camera is drawn onto one shared <canvas> rather than as its own DOM
// element: with ~5,000 cameras, per-marker DOM (and a React component and
// Leaflet popup for each) made every zoom and pan crawl. The icon is
// rendered to an SVG once, rasterised into an <img>, and stamped onto the
// canvas in the middle of each circle.
function renderIconSvg(icon) {
  const container = document.createElement('div')
  const root = createRoot(container)

  flushSync(() => root.render(icon))

  const svg = container.innerHTML
  root.unmount()

  return svg
}

const MARKER_RADIUS = 9
const MARKER_ICON_SIZE = 12

// Stamped onto the canvas from a pre-rendered bitmap, not the SVG itself:
// drawing an SVG <img> re-rasterises it on every call, which across ~5,000
// markers per frame was most of the cost of a zoom. Rendered at the
// screen's pixel density so the icon stays crisp.
const cameraIconBitmap = (async () => {
  const image = new Image()
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(
    renderIconSvg(<IoCameraOutline color="#fff" size={MARKER_ICON_SIZE} />)
  )}`

  await image.decode()

  const scale = window.devicePixelRatio || 1
  const bitmap = document.createElement('canvas')
  bitmap.width = bitmap.height = Math.ceil(MARKER_ICON_SIZE * scale)
  bitmap.getContext('2d').drawImage(image, 0, 0, bitmap.width, bitmap.height)

  return bitmap
})().catch(() => null)

// Set once the bitmap is ready - markers wait for it, or the first frame
// would paint circles with no icons in them.
let CAMERA_ICON = null

const CameraDot = L.CircleMarker.extend({
  _updatePath() {
    L.CircleMarker.prototype._updatePath.call(this)

    const ctx = this._renderer._ctx

    if (!ctx || !CAMERA_ICON || this._empty()) return

    const { x, y } = this._point
    const half = MARKER_ICON_SIZE / 2

    ctx.drawImage(CAMERA_ICON, x - half, y - half, MARKER_ICON_SIZE, MARKER_ICON_SIZE)
  },
})

function markerStyle(camera) {
  return {
    radius: MARKER_RADIUS,
    weight: 2,
    color: '#2b2b2b',
    fillColor: trafficColor(camera.vehicles),
    fillOpacity: 0.9,
  }
}

// Owns the camera markers directly in Leaflet rather than as thousands of
// React children. Each poll updates markers in place - restyling ones that
// changed, adding new ones, dropping ones that are gone - instead of
// tearing them all down and rebuilding.
function CameraLayer({ cameras, onSelect }) {
  const map = useMap()
  const [iconReady, setIconReady] = useState(false)
  const [layer] = useState(() => ({
    renderer: L.canvas({ padding: 0.5 }),
    group: L.layerGroup(),
    dots: new Map(),
  }))

  useEffect(() => {
    let cancelled = false
    cameraIconBitmap.then((bitmap) => {
      CAMERA_ICON = bitmap
      if (!cancelled) setIconReady(true)
    })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    layer.group.addTo(map)
    return () => {
      layer.group.remove()
    }
  }, [map, layer])

  useEffect(() => {
    if (!iconReady) return

    const seen = new Set()

    for (const camera of cameras) {
      seen.add(camera.id)

      let dot = layer.dots.get(camera.id)

      if (!dot) {
        dot = new CameraDot([camera.latitude, camera.longitude], {
          ...markerStyle(camera),
          renderer: layer.renderer,
        })
        dot.on('click', () => onSelect(dot.camera))
        dot.addTo(layer.group)
        layer.dots.set(camera.id, dot)
      } else {
        const style = markerStyle(camera)

        if (dot.options.fillColor !== style.fillColor) dot.setStyle(style)

        const { lat, lng } = dot.getLatLng()
        if (lat !== camera.latitude || lng !== camera.longitude) {
          dot.setLatLng([camera.latitude, camera.longitude])
        }
      }

      dot.camera = camera
    }

    for (const [id, dot] of layer.dots) {
      if (!seen.has(id)) {
        dot.remove()
        layer.dots.delete(id)
      }
    }
  }, [cameras, iconReady, layer, onSelect])

  return null
}

function TrafficHistory({ cameraId }) {
  const [points, setPoints] = useState([])
  const [hoverIndex, setHoverIndex] = useState(null)

  useEffect(() => {
    let cancelled = false

    const load = () => {
      fetch(`/api/cameras/${cameraId}/history`)
        .then((response) => response.json())
        .then((data) => {
          if (!cancelled) setPoints(data)
        })
        .catch(() => {})
    }

    load()

    const timer = setInterval(load, POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [cameraId])

  if (points.length < 2) {
    return <p className="history-empty">Not enough history yet.</p>
  }

  const width = 260
  const height = 60
  const values = points.map((point) => point.v)
  const max = Math.max(...values, 1)

  const xForIndex = (i) => (i / (points.length - 1)) * width
  const yForValue = (v) => height - (v / max) * height

  const coords = points.map((point, i) => `${xForIndex(i).toFixed(1)},${yForValue(point.v).toFixed(1)}`)

  const setHoverFromClientX = (clientX, rect) => {
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    setHoverIndex(Math.round(ratio * (points.length - 1)))
  }

  const handleMouseMove = (event) => {
    setHoverFromClientX(event.clientX, event.currentTarget.getBoundingClientRect())
  }

  const handleTouchMove = (event) => {
    const touch = event.touches[0]
    if (touch) setHoverFromClientX(touch.clientX, event.currentTarget.getBoundingClientRect())
  }

  const hovered = hoverIndex !== null ? points[hoverIndex] : null

  return (
    <div className="history">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="history-graph"
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHoverIndex(null)}
        onTouchStart={handleTouchMove}
        onTouchMove={handleTouchMove}
        onTouchEnd={() => setHoverIndex(null)}
      >
        <polyline points={coords.join(' ')} />
        {hovered && (
          <>
            <line
              className="history-hover-line"
              x1={xForIndex(hoverIndex)}
              x2={xForIndex(hoverIndex)}
              y1={0}
              y2={height}
            />
            <circle
              className="history-hover-dot"
              cx={xForIndex(hoverIndex)}
              cy={yForValue(hovered.v)}
              r={3}
            />
          </>
        )}
      </svg>

      {hovered && (
        <div
          className="history-tooltip"
          style={{
            left: `${(xForIndex(hoverIndex) / width) * 100}%`,
            // Slide the label's anchor with the cursor - left-aligned at the
            // chart's start, centred in the middle, right-aligned at its end -
            // so it never overhangs the panel, which clips anything outside.
            transform: `translate(-${(xForIndex(hoverIndex) / width) * 100}%, -100%)`,
          }}
        >
          <strong>{hovered.v}</strong> vehicles at {hovered.t.slice(11, 16)}
        </div>
      )}

      <div className="history-caption">
        <span>{points[0].t.slice(11, 16)}</span>
        <span>peak {max}</span>
        <span>{points[points.length - 1].t.slice(11, 16)}</span>
      </div>
    </div>
  )
}

function CameraPanel({ camera, onClose }) {
  // Keep rendering the last selected camera's data while closing, so the
  // panel has something to show while it slides out instead of going blank.
  const [displayCamera, setDisplayCamera] = useState(camera)
  const [open, setOpen] = useState(false)
  const [imageExpanded, setImageExpanded] = useState(false)

  // Only the initial load per camera should show the placeholder - not
  // every periodic cache-busted refresh of the same feed.
  const [imageLoaded, setImageLoaded] = useState(false)

  useEffect(() => {
    if (camera) {
      setDisplayCamera(camera)
      // Mount in the closed position first, then flip to open on the next
      // frame so the browser actually animates the transition in rather
      // than just appearing already-open.
      const raf = requestAnimationFrame(() => setOpen(true))
      return () => cancelAnimationFrame(raf)
    }

    setOpen(false)
  }, [camera])

  useEffect(() => {
    // Reset per-camera UI state (lightbox, loading placeholder) whenever
    // the selected camera changes.
    setImageExpanded(false)
    setImageLoaded(false)
  }, [displayCamera?.id])

  // Camera feeds are single static images at a fixed URL - re-fetch on a
  // timer via a cache-busting query param rather than relying on the
  // browser to notice the source has changed.
  const [refreshedAt, setRefreshedAt] = useState(() => Date.now())

  useEffect(() => {
    if (!camera) return

    const timer = setInterval(() => setRefreshedAt(Date.now()), IMAGE_REFRESH_MS)

    return () => clearInterval(timer)
  }, [camera?.id])

  if (!displayCamera) return null

  const imageSrc = displayCamera.image_url
    ? `${displayCamera.image_url}${displayCamera.image_url.includes('?') ? '&' : '?'}t=${refreshedAt}`
    : displayCamera.image_url

  return (
    <aside className={`panel ${open ? 'panel-open' : ''}`}>
      <button className="panel-close" onClick={onClose} aria-label="Close">
        &times;
      </button>

      <div className="panel-image-frame">
        {!imageLoaded && (
          <div className="panel-image-loading">Loading…</div>
        )}

        <img
          className="panel-image"
          key={displayCamera.id}
          src={imageSrc}
          alt={displayCamera.name || `Camera ${displayCamera.id}`}
          onClick={() => setImageExpanded(true)}
          onLoad={() => setImageLoaded(true)}
          onError={() => setImageLoaded(true)}
          style={{ visibility: imageLoaded ? 'visible' : 'hidden' }}
        />
      </div>

      <h2 className="panel-title">
        {displayCamera.name || `Camera ${displayCamera.id}`}
        <SourceBadge source={displayCamera.source} />
      </h2>

      <dl>
        <dt>Road</dt>
        <dd>{displayCamera.road || '—'}</dd>

        <dt>Direction</dt>
        <dd>{displayCamera.direction || '—'}</dd>

        <dt>Source</dt>
        <dd>{displayCamera.source}</dd>

        <dt>Vehicles</dt>
        <dd>{displayCamera.vehicles ?? '—'}</dd>
      </dl>

      <h3>Traffic history</h3>
      <TrafficHistory cameraId={displayCamera.id} />

      {imageExpanded && createPortal(
        // Rendered outside `.panel` via a portal - `.panel`'s slide-in
        // `transform` would otherwise make this fixed overlay position
        // itself relative to the panel's box instead of the viewport.
        <div className="image-lightbox" onClick={() => setImageExpanded(false)}>
          <button
            className="lightbox-close"
            onClick={() => setImageExpanded(false)}
            aria-label="Close"
          >
            &times;
          </button>
          <img key={displayCamera.id} src={imageSrc} alt={displayCamera.name || `Camera ${displayCamera.id}`} />
        </div>,
        document.body
      )}
    </aside>
  )
}

// Sits bottom-left, opposite Leaflet's own attribution. Providers require
// their credit to be shown, so the list of names is always visible and the
// toggle only expands the full statements rather than hiding the credit.
function SourceCredits() {

  const [open, setOpen] = useState(false)

  return (
    <div className="source-credits">

      <button
        className="source-credits-toggle"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span>
          Camera data:{' '}
          {SOURCE_CREDITS.map((credit) => CREDIT_LABELS[credit.source]).join(' \u00b7 ')}
        </span>
        <span aria-hidden="true">{open ? '\u2715' : '\u24d8'}</span>
      </button>

      {open && (
        <dl className="source-credits-detail">
          {SOURCE_CREDITS.map((credit) => (
            <div key={credit.source}>
              <dt>
                <a href={credit.href} target="_blank" rel="noreferrer">
                  {CREDIT_LABELS[credit.source]}
                </a>
              </dt>
              <dd>{credit.lines.join(' ')}</dd>
            </div>
          ))}
        </dl>
      )}

    </div>
  )
}

function App() {
  const [cameras, setCameras] = useState([])
  const [selected, setSelected] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [hiddenSources, setHiddenSources] = useState(() => new Set())

  const sources = useMemo(
    () => [...new Set(cameras.map((camera) => camera.source))].sort(
      (a, b) => regionRank(a) - regionRank(b) || a.localeCompare(b)
    ),
    [cameras]
  )

  const sourceCounts = useMemo(() => {
    const counts = {}

    for (const camera of cameras) {
      counts[camera.source] = (counts[camera.source] || 0) + 1
    }

    return counts
  }, [cameras])

  const visibleCameras = useMemo(
    () => cameras.filter((camera) => !hiddenSources.has(camera.source)),
    [cameras, hiddenSources]
  )

  const toggleSource = (source) => {
    setHiddenSources((prev) => {
      const next = new Set(prev)

      if (next.has(source)) {
        next.delete(source)
      } else {
        next.add(source)
      }

      return next
    })
  }

  const loadCameras = () => {
    setRefreshing(true)

    return fetch('/api/cameras')
      .then((response) => response.json())
      .then(setCameras)
      .catch(() => {})
      .finally(() => setRefreshing(false))
  }

  useEffect(() => {
    loadCameras()

    const timer = setInterval(loadCameras, POLL_INTERVAL_MS)

    return () => clearInterval(timer)
  }, [])

  // Keep the open panel's data (e.g. vehicle count) fresh as polls come in
  useEffect(() => {
    if (!selected) return

    const updated = cameras.find((camera) => camera.id === selected.id)

    if (updated) setSelected(updated)
  }, [cameras])

  return (
    <div className="app">
      {/* Visually hidden (not display:none, so it's still crawlable/accessible
          to screen readers) - the map itself has no room for visible prose,
          but search engines otherwise see nothing but a canvas. */}
      <section className="visually-hidden">
        <h1>UK Traffic Cameras</h1>
        <p>
          OpenHighways is a free map of traffic cameras across the UK.
          Browse road cameras to see current road conditions and traffic
          information from available public camera sources.
        </p>
        <h2>UK Road Camera Map</h2>
        <p>
          Find traffic cameras across motorways and major roads in the UK.
          OpenHighways brings camera data from National Highways, Transport
          for London, Traffic Wales, Traffic Scotland and TrafficWatchNI
          into one easy-to-use map.
        </p>
        <h2>National Highways Traffic Cameras</h2>
        <p>
          Explore traffic cameras located on roads managed by National
          Highways, including major motorways and strategic roads across
          England.
        </p>
      </section>

      <div className="top-left-panel">
        <img className="app-logo" src="/logo.png" alt="OpenHighways" />

        <button
          className="refresh-button"
          onClick={loadCameras}
          disabled={refreshing}
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>

        {sources.length > 0 && (
          <div className="source-filter">
            {sources.map((source) => {
              const label = REGION_LABELS[source] || SOURCE_LABELS[source] || source

              return (
                <label key={source} className="source-filter-item">
                  <input
                    type="checkbox"
                    checked={!hiddenSources.has(source)}
                    onChange={() => toggleSource(source)}
                  />
                  <span>{label}</span>
                  <span className="source-filter-count">{sourceCounts[source] ?? 0}</span>
                </label>
              )
            })}
          </div>
        )}

        <div className="app-footer">
          <a href="https://jamesdillon.uk" target="_blank" rel="noreferrer">jamesdillon.uk</a>
          <span>&middot;</span>
          <span>{APP_VERSION}</span>
          <span>&middot;</span>
          {/* The same API this map runs on is public and documented - the
              footer is the only place a visitor would think to look for it. */}
          <a href="/api/docs" target="_blank" rel="noreferrer">API</a>
          <span>&middot;</span>
          <a href={REPO_URL} target="_blank" rel="noreferrer">GitHub</a>
        </div>
      </div>

      <MapContainer
        center={UK_CENTER}
        zoom={6}
        zoomControl={false}
        className="map"
      >
        <ZoomControl position="topright" />
        <LocateControl />
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        <CameraLayer cameras={visibleCameras} onSelect={setSelected} />

        {/* One popup that follows the selection, not one bound to every
            marker - thousands of idle Leaflet popups were part of the lag. */}
        {selected && (
          <Popup
            key={selected.id}
            position={[selected.latitude, selected.longitude]}
            offset={[0, -MARKER_RADIUS]}
          >
            {selected.name || selected.id}
          </Popup>
        )}
      </MapContainer>

      <SourceCredits />

      <CameraPanel camera={selected} onClose={() => setSelected(null)} />
    </div>
  )
}

export default App

