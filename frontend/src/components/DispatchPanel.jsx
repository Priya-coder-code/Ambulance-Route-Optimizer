import { useState } from 'react';
import { api } from '../api.js';
import { fmtDist, fmtInt, fmtTime, STATUS_LABEL } from '../format.js';

function LocationSearch({ setPatient, setFocus, say }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);

  const search = async () => {
    if (q.trim().length < 3) return say('Type at least 3 characters');
    setLoading(true);
    try {
      const r = await api.geocode(q);
      setResults(r.results);
      if (!r.results.length) say('No address found inside the service area');
    } catch (e) { say('⚠ ' + e.message, 6000); }
    finally { setLoading(false); }
  };

  const pick = (r) => {
    setPatient({ lat: r.lat, lon: r.lon, address: r.label });
    setFocus({ center: [r.lat, r.lon], zoom: 16 });
    setResults(null);
    setQ('');
  };

  return (
    <div className="search">
      <div className="row">
        <input type="text" value={q} placeholder="Search address, e.g. India Gate" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && search()} />
        <button onClick={search} disabled={loading}>{loading ? '…' : 'Search'}</button>
      </div>
      {results && results.length > 0 && (
        <ul className="results">
          {results.map((r, i) => (
            <li key={i} onClick={() => pick(r)}>{r.label}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

const AMB_STATUS = {
  selected: ['Selected', 'ok'], reachable: ['Slower', ''], slower: ['Stopped early', 'dim'],
  pruned: ['Skipped (bound)', 'dim'], unreachable: ['No route', 'bad'],
};

function FacilityChips({ list, meta, required }) {
  return (
    <span className="chips">
      {(list || []).map((f) => (
        <span key={f} className={`chip ${required?.includes(f) ? 'req' : ''}`}>{meta?.facilities?.[f] || f}</span>
      ))}
    </span>
  );
}

function Decision({ d, meta, onConfirm, onComplete, onCancel, onNew }) {
  const c = d.candidates || {};
  const alg = d.algorithm || {};
  const em = d.emergency;
  return (
    <div className="decision">
      <div className="decision-head">
        <div>
          <div className="muted small">Case #{em?.id} · {em?.emergency_label} · {em?.severity}</div>
          <div className="muted small">{em?.address || `${d.patient.lat.toFixed(5)}, ${d.patient.lon.toFixed(5)}`}</div>
        </div>
        <span className={`pill status-${d.status}`}>{STATUS_LABEL[d.status]}</span>
      </div>

      <ol className="answers">
        <li><span className="q">Ambulance</span>
          <b>{d.ambulance.code}</b> <span className={`kind ${d.ambulance.kind}`}>{d.ambulance.kind}</span>
          <div className="muted small">{d.ambulance.base_name}</div></li>
        <li><span className="q">Route to patient</span>
          <b style={{ color: 'var(--leg1)' }}>{fmtTime(d.ambulance.eta_s)}</b> · {fmtDist(d.ambulance.distance_m)}
          {d.ambulance.roads?.length > 0 && <div className="muted small">via {d.ambulance.roads.slice(0, 5).join(' → ')}</div>}</li>
        <li><span className="q">Hospital</span>
          <b>{d.hospital.name}</b>
          <div><FacilityChips list={d.hospital.facilities} meta={meta} required={c.required_facilities} /></div></li>
        <li><span className="q">Route to hospital</span>
          <b style={{ color: 'var(--leg2)' }}>{fmtTime(d.hospital.eta_s)}</b> · {fmtDist(d.hospital.distance_m)}
          {d.hospital.roads?.length > 0 && <div className="muted small">via {d.hospital.roads.slice(0, 5).join(' → ')}</div>}</li>
        <li className="total"><span className="q">Total travel time</span>
          <b>{fmtTime(d.total.eta_s)}</b> · {fmtDist(d.total.distance_m)}</li>
      </ol>

      {d.warnings?.map((w, i) => <div key={i} className="warn">⚠ {w}</div>)}

      <div className="row buttons">
        {d.status === 'planned' && <button className="primary" onClick={onConfirm}>Confirm &amp; dispatch</button>}
        {d.status === 'dispatched' && <button className="primary" onClick={onComplete}>Patient delivered</button>}
        {(d.status === 'planned' || d.status === 'dispatched') && <button className="secondary" onClick={onCancel}>Cancel</button>}
        <button className="secondary" onClick={onNew}>New emergency</button>
      </div>

      {c.ambulances && (
        <details open>
          <summary>Why this ambulance? <span className="muted">({c.ambulances.length} compared)</span></summary>
          <table className="cmp">
            <thead><tr><th>Ambulance</th><th>Straight</th><th>Road ETA</th><th>Result</th></tr></thead>
            <tbody>
              {c.ambulances.map((a) => {
                const [label, cls] = AMB_STATUS[a.status] || [a.status, ''];
                return (
                  <tr key={a.id} className={a.status === 'selected' ? 'sel' : ''} title={a.note || ''}>
                    <td>{a.code} <span className={`kind ${a.kind}`}>{a.kind}</span></td>
                    <td>{fmtDist(a.straight_m)}</td>
                    <td>{a.eta_s != null ? fmtTime(a.eta_s) : '–'}</td>
                    <td className={cls}>{label}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {c.excluded_ambulances?.length > 0 && (
            <p className="muted small">{c.excluded_ambulances.length} basic (BLS) ambulances not considered: this case needs advanced life support.</p>
          )}
          <p className="muted small">The nearest ambulance in a straight line is not always the fastest by road.
            “Skipped (bound)” = even driving a straight line at top speed it could not beat the best time, so Dijkstra was not run.
            “Stopped early” = its Dijkstra search was cut off once it could no longer win.</p>
        </details>
      )}

      {c.hospitals && (
        <details open>
          <summary>Why this hospital?</summary>
          <p className="small">Needs: <FacilityChips list={c.required_facilities} meta={meta} required={c.required_facilities} />
            <span className="muted"> · {c.suitable_hospital_count} suitable hospitals</span></p>
          <table className="cmp">
            <thead><tr><th>Suitable hospital</th><th>ETA</th></tr></thead>
            <tbody>
              {c.hospitals.map((h) => (
                <tr key={h.id} className={h.selected ? 'sel' : ''}>
                  <td>{h.name}<div className="muted small">{h.beds_available} beds free</div></td>
                  <td>{fmtTime(h.eta_s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {c.skipped_hospitals?.length > 0 && (
            <>
              <p className="small" style={{ marginTop: 10 }}><b>Closer, but not suitable:</b></p>
              <table className="cmp">
                <tbody>
                  {c.skipped_hospitals.map((h) => (
                    <tr key={h.id}><td>{h.name}</td><td>{fmtTime(h.eta_s)}</td><td className="bad">{h.reason}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </details>
      )}

      {alg.dijkstra_runs != null && (
        <details>
          <summary>Algorithm statistics</summary>
          <table className="cmp">
            <tbody>
              <tr><td>Algorithm</td><td>{alg.name}</td></tr>
              <tr><td>Dijkstra runs</td><td>{alg.dijkstra_runs}</td></tr>
              <tr><td>Ambulances compared</td><td>{alg.ambulances_considered} ({alg.ambulances_pruned} skipped by bound)</td></tr>
              <tr><td>Nodes settled</td><td>{fmtInt(alg.nodes_settled)}</td></tr>
              <tr><td>Edges relaxed</td><td>{fmtInt(alg.edges_relaxed)}</td></tr>
              <tr><td>Road graph</td><td>{fmtInt(alg.graph_nodes)} nodes · {fmtInt(alg.graph_edges)} edges</td></tr>
              <tr><td>Traffic</td><td>{meta?.traffic_profiles?.[alg.traffic_profile] || alg.traffic_profile}</td></tr>
              <tr><td>Computation time</td><td><b>{alg.compute_ms} ms</b></td></tr>
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

export default function DispatchPanel({ meta, patient, setPatient, form, setForm, decision, onFind, ready, setFocus,
  showSearch, setShowSearch, trafficLabel, onConfirm, onComplete, onCancel, onNew, say }) {
  if (decision) {
    return (
      <>
        <Decision d={decision} meta={meta} onConfirm={onConfirm} onComplete={onComplete} onCancel={onCancel} onNew={onNew} />
        <label className="check"><input type="checkbox" checked={showSearch} onChange={(e) => setShowSearch(e.target.checked)} /> Show area searched by Dijkstra</label>
      </>
    );
  }
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const types = meta?.emergency_types || [];
  const selType = types.find((t) => t.code === form.emergency_type);

  return (
    <>
      <section>
        <h3><span className="step">1</span>Patient location</h3>
        <LocationSearch setPatient={setPatient} setFocus={setFocus} say={say} />
        {patient ? (
          <div className="picked">
            <b>{patient.address || 'Looking up address…'}</b>
            <div className="muted small">{patient.lat.toFixed(5)}, {patient.lon.toFixed(5)}</div>
          </div>
        ) : (
          <p className="muted small">…or click the patient’s location on the map.</p>
        )}
      </section>

      <section>
        <h3><span className="step">2</span>Emergency type</h3>
        <div className="types">
          {types.map((t) => (
            <button key={t.code} className={`type ${form.emergency_type === t.code ? 'active' : ''}`}
              onClick={() => setForm((f) => ({ ...f, emergency_type: t.code }))} title={t.description}>
              {t.label}
            </button>
          ))}
        </div>
        {selType && (
          <p className="muted small">Needs a hospital with: {selType.required_facilities.map((f) => meta.facilities[f]).join(', ')}
            {selType.needs_als ? ' · advanced (ALS) ambulance' : ''}</p>
        )}
        <div className="seg">
          {Object.entries(meta?.severities || {}).map(([k, label]) => (
            <button key={k} className={form.severity === k ? `active ${k}` : ''} onClick={() => setForm((f) => ({ ...f, severity: k }))}>{label}</button>
          ))}
        </div>
      </section>

      <section>
        <details>
          <summary>Patient details (optional)</summary>
          <div className="row"><input type="text" placeholder="Patient name" value={form.patient_name} onChange={set('patient_name')} /></div>
          <div className="row"><input type="text" placeholder="Caller phone" value={form.phone} onChange={set('phone')} /></div>
          <div className="row"><input type="text" placeholder="Notes" value={form.notes} onChange={set('notes')} /></div>
        </details>
      </section>

      <section>
        <button className="primary big" disabled={!ready || !patient || !form.emergency_type} onClick={onFind}>
          Find optimal dispatch
        </button>
        <p className="muted small">Traffic: {trafficLabel || '–'} (change in the Roads tab)</p>
        <label className="check"><input type="checkbox" checked={showSearch} onChange={(e) => setShowSearch(e.target.checked)} /> Show area searched by Dijkstra</label>
      </section>
    </>
  );
}
