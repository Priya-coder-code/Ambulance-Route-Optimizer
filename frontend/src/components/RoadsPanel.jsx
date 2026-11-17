import { api } from '../api.js';
import { fmtClock } from '../format.js';

export default function RoadsPanel({ meta, roadState, setRoadState, tool, setTool, toolOpts, setToolOpts, setFocus, say,
  replan, showRoads, setShowRoads }) {
  const setTraffic = async (p) => {
    try {
      await api.setTraffic(p);
      setRoadState((s) => ({ ...s, traffic_profile: p }));
      say(`Traffic set to: ${meta.traffic_profiles[p]}`);
      await replan();
    } catch (e) { say('⚠ ' + e.message); }
  };
  const remove = async (id) => {
    try {
      const r = await api.deleteCondition(id);
      setRoadState((s) => ({ ...s, ...r }));
      await replan();
    } catch (e) { say('⚠ ' + e.message); }
  };

  return (
    <>
      <section>
        <h3>Traffic level</h3>
        <div className="seg">
          {Object.entries(meta?.traffic_profiles || {}).map(([k, label]) => (
            <button key={k} className={roadState.traffic_profile === k ? 'active' : ''} onClick={() => setTraffic(k)}>{label}</button>
          ))}
        </div>
        <p className="muted small">Main roads slow down more than side streets at peak hour, so the fastest
          ambulance and route can change.</p>
      </section>

      <section>
        <h3>Report a road problem</h3>
        <div className="modes">
          <label className={tool === 'closure' ? 'on' : ''}>
            <input type="radio" name="tool" checked={tool === 'closure'} onChange={() => setTool('closure')} />
            <b>Road closed</b><small>accident, construction – click the road</small>
          </label>
          <label className={tool === 'congestion' ? 'on' : ''}>
            <input type="radio" name="tool" checked={tool === 'congestion'} onChange={() => setTool('congestion')} />
            <b>Traffic jam</b><small>roads within 400 m get slower</small>
          </label>
        </div>
        {tool === 'congestion' && (
          <div className="row">
            <span className="lbl">Slow-down</span>
            <select value={toolOpts.factor} onChange={(e) => setToolOpts((o) => ({ ...o, factor: +e.target.value }))}>
              <option value={2}>2× slower</option>
              <option value={3}>3× slower</option>
              <option value={5}>5× slower</option>
            </select>
          </div>
        )}
        {tool && <button className="secondary" onClick={() => setTool(null)}>Done</button>}
        <p className="muted small">Saved in PostgreSQL. If a case is still being planned, its route is re-calculated automatically.</p>
      </section>

      <section>
        <h3>Active road conditions ({roadState.conditions.length})</h3>
        {roadState.conditions.length === 0 && <p className="muted small">None.</p>}
        <ul className="list">
          {roadState.conditions.map((c) => (
            <li key={c.id}>
              <div className="grow" onClick={() => setFocus({ center: [c.lat, c.lon], zoom: 16 })}>
                <b>{c.kind === 'closure' ? 'Road closed' : `Traffic jam ${c.factor}×`}</b>
                <div className="muted small">{fmtClock(c.created_at)} · {c.lat.toFixed(4)}, {c.lon.toFixed(4)}</div>
              </div>
              <button className="secondary" onClick={() => remove(c.id)}>Remove</button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <label className="check"><input type="checkbox" checked={showRoads} onChange={(e) => setShowRoads(e.target.checked)} />
          Show the road graph (edges) when zoomed in</label>
      </section>
    </>
  );
}
