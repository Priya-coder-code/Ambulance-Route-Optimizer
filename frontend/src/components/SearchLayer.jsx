import { useEffect } from 'react';
import L from 'leaflet';
import { useMap } from 'react-leaflet';

/* Draws the nodes Dijkstra settled (thousands of points) on a canvas, animated
   in the order they were settled - you can see the search grow outwards. */
const PointsLayer = L.Layer.extend({
  initialize(sets) { this.sets = sets; this.count = 0; this.total = sets.reduce((s, x) => s + x.pts.length, 0); },
  onAdd(map) {
    this._map = map;
    this._canvas = L.DomUtil.create('canvas', 'pts-layer');
    map.getPane('search').appendChild(this._canvas);
    map.on('moveend zoomend resize viewreset', this._reset, this);
    map.on('zoomstart', this._hide, this);
    this._reset();
    this._animate();
  },
  onRemove(map) {
    cancelAnimationFrame(this._raf);
    L.DomUtil.remove(this._canvas);
    map.off('moveend zoomend resize viewreset', this._reset, this);
    map.off('zoomstart', this._hide, this);
  },
  _hide() { this._canvas.style.display = 'none'; },
  _animate() {
    const step = Math.max(1, Math.ceil(this.total / 90));  // ~1.5 s
    const tick = () => {
      const from = this.count;
      this.count = Math.min(this.total, this.count + step);
      this._draw(from, this.count);
      if (this.count < this.total) this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  },
  _reset() {
    const map = this._map; if (!map) return;
    const size = map.getSize(), c = this._canvas, dpr = window.devicePixelRatio || 1;
    c.width = size.x * dpr; c.height = size.y * dpr;
    c.style.width = size.x + 'px'; c.style.height = size.y + 'px'; c.style.display = '';
    L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
    this._ctx = c.getContext('2d');
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._draw(0, this.count);
  },
  _draw(a, b) {
    const ctx = this._ctx, map = this._map; if (!ctx) return;
    const z = map.getZoom();
    const r = z >= 16 ? 2.6 : z >= 14 ? 1.9 : 1.3;
    let offset = 0;
    for (const set of this.sets) {
      const lo = Math.max(a - offset, 0), hi = Math.min(b - offset, set.pts.length);
      ctx.fillStyle = set.color;
      for (let i = lo; i < hi; i++) {
        const p = map.latLngToContainerPoint(set.pts[i]);
        ctx.fillRect(p.x - r, p.y - r, 2 * r, 2 * r);
      }
      offset += set.pts.length;
    }
  },
});

export default function SearchLayer({ search }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane('search')) map.createPane('search').style.zIndex = 350;
    const layer = new PointsLayer([
      { pts: search.ambulance || [], color: '#8fbaff' },
      { pts: search.hospital || [], color: '#ffbd8a' },
    ]).addTo(map);
    return () => { layer.remove(); };
  }, [search, map]);
  return null;
}
