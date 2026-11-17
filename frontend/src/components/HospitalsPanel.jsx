import { useState } from 'react';
import { api } from '../api.js';

export default function HospitalsPanel({ hospitals, meta, refresh, tool, setTool, setFocus, say }) {
  const [q, setQ] = useState('');
  const [fac, setFac] = useState('');
  const [open, setOpen] = useState(null);
  const facilities = meta?.facilities || {};

  const list = hospitals.filter((h) =>
    (!q || h.name.toLowerCase().includes(q.toLowerCase())) && (!fac || h.facilities.includes(fac)));

  const patch = async (h, p) => {
    try { await api.updateHospital(h.id, p); refresh(); }
    catch (e) { say('⚠ ' + e.message); }
  };
  const toggle = (h, f) => {
    const next = h.facilities.includes(f) ? h.facilities.filter((x) => x !== f) : [...h.facilities, f];
    patch(h, { facilities: next });
  };

  return (
    <>
      <section>
        <h3>Hospitals ({hospitals.length})</h3>
        <p className="muted small">Real hospitals from OpenStreetMap. Facilities and free beds are <b>simulated</b> –
          click a hospital to edit them.</p>
        <div className="row"><input type="text" placeholder="Search hospital" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <div className="row">
          <select value={fac} onChange={(e) => setFac(e.target.value)}>
            <option value="">All facilities</option>
            {Object.entries(facilities).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <button className={tool === 'add-hospital' ? 'primary' : 'secondary'} style={{ flex: 'none' }}
            onClick={() => setTool(tool === 'add-hospital' ? null : 'add-hospital')}>
            {tool === 'add-hospital' ? 'Click map…' : '+ Add'}
          </button>
        </div>
      </section>
      <section>
        <ul className="list">
          {list.slice(0, 300).map((h) => (
            <li key={h.id} className={`col ${h.is_active ? '' : 'inactive'}`}>
              <div className="grow" onClick={() => { setOpen(open === h.id ? null : h.id); setFocus({ center: [h.lat, h.lon], zoom: 16 }); }}>
                <div className="between"><b>{h.name}</b><span className="muted small">{h.beds_available} beds</span></div>
                <span className="chips">
                  {h.facilities.length ? h.facilities.map((f) => <span key={f} className="chip">{facilities[f] || f}</span>)
                    : <span className="chip off">No emergency department</span>}
                </span>
              </div>
              {open === h.id && (
                <div className="edit">
                  <div className="checks">
                    {Object.entries(facilities).map(([k, v]) => (
                      <label key={k}><input type="checkbox" checked={h.facilities.includes(k)} onChange={() => toggle(h, k)} /> {v}</label>
                    ))}
                  </div>
                  <div className="row">
                    <span className="lbl">Free beds</span>
                    <button className="secondary" onClick={() => patch(h, { beds_available: h.beds_available - 1 })}>−</button>
                    <b style={{ minWidth: 28, textAlign: 'center' }}>{h.beds_available}</b>
                    <button className="secondary" onClick={() => patch(h, { beds_available: h.beds_available + 1 })}>+</button>
                    <label className="check" style={{ marginLeft: 'auto' }}>
                      <input type="checkbox" checked={h.is_active} onChange={(e) => patch(h, { is_active: e.target.checked })} /> active
                    </label>
                  </div>
                  <div className="muted small">{h.osm_id ? `OpenStreetMap ${h.osm_id}` : `source: ${h.source}`}{h.address ? ` · ${h.address}` : ''}</div>
                </div>
              )}
            </li>
          ))}
        </ul>
        {list.length > 300 && <p className="muted small">Showing 300 of {list.length} – refine the search.</p>}
      </section>
    </>
  );
}
