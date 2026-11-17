import { useState } from 'react';
import { api } from '../api.js';

export default function FleetPanel({ ambulances, refresh, tool, setTool, toolOpts, setToolOpts, setFocus, say }) {
  const [filter, setFilter] = useState('all');
  const counts = ambulances.reduce((m, a) => ({ ...m, [a.status]: (m[a.status] || 0) + 1 }), {});
  const list = ambulances.filter((a) => filter === 'all' || a.status === filter);

  const setStatus = async (a, status) => {
    try { await api.updateAmbulance(a.id, { status }); refresh(); }
    catch (e) { say('⚠ ' + e.message); }
  };

  return (
    <>
      <section>
        <h3>Ambulance fleet</h3>
        <div className="kpis">
          <div><b>{counts.available || 0}</b><span>available</span></div>
          <div><b>{counts.busy || 0}</b><span>busy</span></div>
          <div><b>{counts.maintenance || 0}</b><span>maintenance</span></div>
        </div>
        <p className="muted small">Simulated fleet stored in PostgreSQL. <b>A+</b> = advanced life support (ALS),
          needed for critical cases; <b>A</b> = basic (BLS).</p>
        <div className="row">
          <button className={tool === 'add-ambulance' ? 'primary' : 'secondary'}
            onClick={() => setTool(tool === 'add-ambulance' ? null : 'add-ambulance')}>
            {tool === 'add-ambulance' ? 'Click the map to place it…' : '+ Add ambulance'}
          </button>
          <select value={toolOpts.kind} onChange={(e) => setToolOpts((o) => ({ ...o, kind: e.target.value }))} style={{ flex: 'none', width: 90 }}>
            <option value="ALS">ALS</option>
            <option value="BLS">BLS</option>
          </select>
        </div>
      </section>
      <section>
        <div className="seg small">
          {['all', 'available', 'busy', 'maintenance'].map((f) => (
            <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
        <ul className="list">
          {list.map((a) => (
            <li key={a.id}>
              <div className="grow" onClick={() => setFocus({ center: [a.lat, a.lon], zoom: 16 })}>
                <b>{a.code}</b> <span className={`kind ${a.kind}`}>{a.kind}</span>
                <div className="muted small">{a.base_name}</div>
              </div>
              <select className={`st-${a.status}`} value={a.status} onChange={(e) => setStatus(a, e.target.value)}>
                <option value="available">available</option>
                <option value="busy">busy</option>
                <option value="maintenance">maintenance</option>
              </select>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
