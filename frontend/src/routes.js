// The map's URLs - one per camera, road and region. The backend renders the
// same URLs as crawlable pages (backend/seo.py) using the same rules, so
// keep the two in step.

// URL slug -> source, one provider per region.
export const REGION_SOURCES = {
  england: 'national_highways',
  london: 'tfl',
  scotland: 'traffic_scotland',
  wales: 'traffic_wales',
  'northern-ireland': 'northern_ireland',
}

const SOURCE_REGIONS = Object.fromEntries(
  Object.entries(REGION_SOURCES).map(([slug, source]) => [source, slug])
)

// Great Britain shares one road numbering, but Northern Ireland has its own
// - its A1 is a different road from the A1 in England.
const NI_SOURCES = new Set(['northern_ireland'])

export function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}

function network(source) {
  return NI_SOURCES.has(source) ? 'ni' : 'gb'
}

export function cameraPath(camera) {
  return `/camera/${camera.id}`
}

export function roadPath(camera) {
  if (!camera.road || !slugify(camera.road)) return null

  const slug = slugify(camera.road)

  return network(camera.source) === 'ni' ? `/road/ni/${slug}` : `/road/${slug}`
}

export function regionPath(source) {
  const slug = SOURCE_REGIONS[source]

  return slug ? `/region/${slug}` : null
}

export function parseRoute(pathname) {
  let match

  if ((match = pathname.match(/^\/camera\/(\d+)$/))) {
    return { type: 'camera', id: Number(match[1]) }
  }

  if ((match = pathname.match(/^\/road\/ni\/([^/]+)$/))) {
    return { type: 'road', network: 'ni', slug: slugify(decodeURIComponent(match[1])) }
  }

  if ((match = pathname.match(/^\/road\/([^/]+)$/))) {
    return { type: 'road', network: 'gb', slug: slugify(decodeURIComponent(match[1])) }
  }

  if ((match = pathname.match(/^\/region\/([^/]+)$/)) && REGION_SOURCES[match[1]]) {
    return { type: 'region', slug: match[1], source: REGION_SOURCES[match[1]] }
  }

  return { type: 'home' }
}

// Whether a camera belongs to a road or region route.
export function inRoute(route, camera) {
  if (route.type === 'road') {
    return Boolean(camera.road) && network(camera.source) === route.network && slugify(camera.road) === route.slug
  }

  if (route.type === 'region') return camera.source === route.source

  return false
}

// M roads, then A roads, then the rest - each in numeric order.
export function compareRoads(a, b) {
  const key = (road) => {
    const match = road.match(/^([A-Za-z]+)(\d+)(.*)$/)

    if (!match) return [3, road, 0, '']

    const prefix = match[1].toUpperCase()

    return [{ M: 0, A: 1 }[prefix] ?? 2, prefix, Number(match[2]), match[3]]
  }

  const [ka, kb] = [key(a), key(b)]

  for (let i = 0; i < ka.length; i++) {
    if (ka[i] < kb[i]) return -1
    if (ka[i] > kb[i]) return 1
  }

  return 0
}
