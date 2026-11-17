import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import {
  Circle, CircleMarker, MapContainer, Marker, Polyline, TileLayer, Tooltip, useMap, useMapEvents,
} from 'react-leaflet';
import { api } from '../api.js';
import { fmtDist, fmtTime } from '../format.js';
import SearchLayer from './SearchLayer.jsx';

const COLORS = { leg1: '#1f5fd6', leg2: '#c8102e', jam: '#e8a800', closed: '#111' };
const DETAIL_ZOOM = 14;

const iconCache = {};
function icon(cls, text, size = 22, anchor = null) {
  const key = `${cls}|${text}|${size}|${anchor}`;
  if (!iconCache[key]) {
    iconCache[key] = L.divIcon({
      className: '', html: `<div class="mk ${cls}">${text}</div>`,
      iconSize: [size, size], iconAnchor: anchor || [size / 2, size / 2],
    });
  }
  return iconCache[key];
}

function ClickAndZoom({ onClick, onZoom, onMove }) {
  const map = useMapEvents({
    click: (e) => onClick(e.latlng.lat, e.latlng.lng),
    zoomend: () => onZoom(map.getZoom()),
    moveend: () => onMove(map),
  });
  return null;
}

function Focus({ focus }) {
  const map = useMap();
  useEffect(() => {
    if (!focus) return;
    if (focus.bounds) map.fitBounds(focus.bounds, { padding: [20, 20] });
    else if (focus.points?.length) map.fitBounds(L.latLngBounds(focus.points), { padding: [60, 60], maxZoom: 16 });
    else if (focus.center) map.flyTo(focus.center, focus.zoom || 16, { duration: 0.6 });
  }, [focus, map]);
  return null;
}

function Route({ coords, color }) {
  if (!coords || coords.length < 2) return null;
  return (
    <>
      <Polyline positions={coords} pathOptions={{ color: '#fff', weight: 9, opacity: 0.9, interactive: false }} />
      <Polyline positions={coords} pathOptions={{ color, weight: 5, opacity: 0.95, interactive: false }} />
    </>
  );
}

export default function MapView({ status, ambulances, hospitals, patient, decision, roadState, focus,
  onMapClick, tool, tab, showSearch, showRoads, say }) {
  const [zoom, setZoom] = useState(12);
  const [roads, setRoads] = useState([]);
  const roadsKey = useRef('');
  const center = status?.area?.center || [28.6315, 77.2167];
  const detailed = zoom >= DETAIL_ZOOM;

  // road graph overlay (only when zoomed in, only the visible part)
  const loadRoads = async (map) => {
    if (!showRoads || map.getZoom() < 14) { setRoads([]); roadsKey.current = ''; return; }
    const b = map.getBounds();
    const q = { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() };
    const key = Object.values(q).map((x) => x.toFixed(4)).join(',');
    if (key === roadsKey.current) return;
    roadsKey.current = key;
    try { setRoads((await api.roads(q)).roads); } catch { /* still loading */ }
  };
  const mapRef = useRef(null);
  useEffect(() => {
    if (mapRef.current) {
      roadsKey.current = '';
      if (showRoads && mapRef.current.getZoom() < 14) say('Zoom in further to see the road graph edges');
      loadRoads(mapRef.current);
    }
  }, [showRoads]); // eslint-disable-line react-hooks/exhaustive-deps

  const hospStyle = useMemo(() => {
    const m = {};
    if (decision?.candidates) {
      for (const h of decision.candidates.hospitals || []) m[h.id] = 'candidate';
      for (const h of decision.candidates.skipped_hospitals || []) m[h.id] = 'skipped';
    }
    if (decision?.hospital?.id) m[decision.hospital.id] = 'selected';
    return m;
  }, [decision]);

  const selAmb = decision?.ambulance?.id;
  const cursor = tool || tab === 'dispatch' ? 'crosshair' : '';

  return (
    <div className={`map ${cursor}`}>
      <MapContainer center={center} zoom={12} preferCanvas zoomSnap={0.5} ref={mapRef} style={{ height: '100%', width: '100%' }}>
        <TileLayer url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" maxZoom={19}
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' />
        <ClickAndZoom onClick={onMapClick} onZoom={setZoom} onMove={loadRoads} />
        <Focus focus={focus} />

        {roads.length > 0 && (
          <Polyline positions={roads} pathOptions={{ color: '#3d4f7a', weight: 1.3, opacity: 0.6, interactive: false }} />
        )}

        {showSearch && decision?.search && <SearchLayer search={decision.search} />}

        {/* road conditions */}
        {roadState.shapes?.congested?.length > 0 && (
          <Polyline positions={roadState.shapes.congested} pathOptions={{ color: COLORS.jam, weight: 6, opacity: 0.8, interactive: false }} />
        )}
        {roadState.shapes?.closed?.length > 0 && (
          <Polyline positions={roadState.shapes.closed} pathOptions={{ color: COLORS.closed, weight: 6, dashArray: '2 7', interactive: false }} />
        )}
        {roadState.conditions.filter((c) => c.kind === 'congestion').map((c) => (
          <Circle key={c.id} center={[c.lat, c.lon]} radius={c.radius_m} bubblingMouseEvents
            pathOptions={{ color: COLORS.jam, weight: 1, fillOpacity: 0.08 }} />
        ))}
        {roadState.conditions.filter((c) => c.kind === 'closure').map((c) => (
          <Marker key={c.id} position={[c.lat, c.lon]} icon={icon('closure', '✕', 18)} bubblingMouseEvents>
            <Tooltip>{c.note || 'Road closed'}</Tooltip>
          </Marker>
        ))}

        {/* routes */}
        {decision && <Route coords={decision.ambulance?.route} color={COLORS.leg1} />}
        {decision && <Route coords={decision.hospital?.route} color={COLORS.leg2} />}

        {/* hospitals */}
        {hospitals.map((h) => {
          const st = hospStyle[h.id];
          const tip = (
            <Tooltip>
              <b>{h.name}</b><br />
              {h.facilities.length ? h.facilities.join(', ') : 'no emergency department'} · {h.beds_available} beds free
            </Tooltip>
          );
          if (st === 'selected') {
            return <Marker key={h.id} position={[h.lat, h.lon]} icon={icon('hospital selected', 'H', 30)} zIndexOffset={900} bubblingMouseEvents>{tip}</Marker>;
          }
          if (detailed || st === 'candidate') {
            const cls = st === 'skipped' ? 'hospital dim' : !h.is_active ? 'hospital dim' : 'hospital';
            return <Marker key={h.id} position={[h.lat, h.lon]} icon={icon(cls, 'H')} bubblingMouseEvents>{tip}</Marker>;
          }
          return (
            <CircleMarker key={h.id} center={[h.lat, h.lon]} radius={4} bubblingMouseEvents
              pathOptions={{ color: '#fff', weight: 1.5, fillColor: st === 'skipped' || !h.is_active ? '#9aa3b5' : COLORS.leg2, fillOpacity: 1 }}>
              {tip}
            </CircleMarker>
          );
        })}

        {/* ambulances */}
        {ambulances.map((a) => {
          const sel = a.id === selAmb;
          const cls = `ambulance ${a.kind.toLowerCase()} ${a.status !== 'available' ? 'busy' : ''} ${sel ? 'selected' : ''}`;
          return (
            <Marker key={a.id} position={[a.lat, a.lon]} icon={icon(cls, a.kind === 'ALS' ? 'A+' : 'A', sel ? 28 : 20, sel ? null : [-2, -2])}
              zIndexOffset={sel ? 1000 : 500} bubblingMouseEvents>
              <Tooltip><b>{a.code}</b> · {a.kind === 'ALS' ? 'Advanced' : 'Basic'} life support<br />{a.base_name}<br />{a.status}</Tooltip>
            </Marker>
          );
        })}

        {/* patient */}
        {patient && (
          <Marker position={[patient.lat, patient.lon]} icon={icon('incident', '!', 28)} zIndexOffset={2000} bubblingMouseEvents>
            <Tooltip>Patient{patient.address ? `: ${patient.address}` : ''}</Tooltip>
          </Marker>
        )}
      </MapContainer>

      <div className="legend">
        <span><i className="mk-mini amb" />ambulance (A+ = advanced)</span>
        <span><i className="mk-mini hosp" />hospital</span>
        <span><i className="ln leg1" />ambulance → patient{decision ? ` · ${fmtTime(decision.ambulance?.eta_s)}` : ''}</span>
        <span><i className="ln leg2" />patient → hospital{decision ? ` · ${fmtTime(decision.hospital?.eta_s)}` : ''}</span>
        {showSearch && <span><i className="sw exp1" /><i className="sw exp2" />area searched by Dijkstra</span>}
        <span><i className="ln closed" />closed</span>
        <span><i className="ln jam" />congested</span>
        {decision && <span className="total">Total {fmtTime(decision.total?.eta_s)} · {fmtDist(decision.total?.distance_m)}</span>}
      </div>
    </div>
  );
}
