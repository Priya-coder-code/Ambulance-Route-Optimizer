import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { fmtInt } from './format.js';
import MapView from './components/MapView.jsx';
import DispatchPanel from './components/DispatchPanel.jsx';
import FleetPanel from './components/FleetPanel.jsx';
import HospitalsPanel from './components/HospitalsPanel.jsx';
import RoadsPanel from './components/RoadsPanel.jsx';
import HistoryPanel from './components/HistoryPanel.jsx';

const TABS = [
  ['dispatch', 'Dispatch'],
  ['fleet', 'Ambulances'],
  ['hospitals', 'Hospitals'],
  ['roads', 'Roads'],
  ['history', 'History'],
];

const EMPTY_FORM = { emergency_type: '', severity: 'serious', patient_name: '', phone: '', notes: '' };

/* Turn either a fresh /plan response or a stored dispatch into one shape for the UI. */
export function toDecision(src, fromHistory = false) {
  if (!fromHistory) {
    return {
      dispatchId: src.dispatch_id, status: src.status, emergency: src.emergency, patient: src.patient,
      ambulance: src.ambulance, hospital: src.hospital, total: src.total, candidates: src.candidates,
      algorithm: src.algorithm, warnings: src.warnings || [], search: src.search,
    };
  }
  const d = src.details || {};
  return {
    dispatchId: src.id, status: src.status, emergency: src.emergency,
    patient: d.patient || { lat: src.emergency.lat, lon: src.emergency.lon },
    ambulance: { ...(d.ambulance || {}), ...(src.ambulance || {}), eta_s: src.eta_to_patient_s,
                 distance_m: src.distance_to_patient_m, route: src.route_to_patient },
    hospital: { ...(d.hospital || {}), ...(src.hospital || {}), eta_s: src.eta_to_hospital_s,
                distance_m: src.distance_to_hospital_m, route: src.route_to_hospital },
    total: { eta_s: src.total_eta_s, distance_m: src.total_distance_m },
    candidates: d.candidates, algorithm: d.algorithm, warnings: d.warnings || [], search: null,
  };
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [meta, setMeta] = useState(null);
  const [ambulances, setAmbulances] = useState([]);
  const [hospitals, setHospitals] = useState([]);
  const [roadState, setRoadState] = useState({ conditions: [], shapes: { closed: [], congested: [] }, traffic_profile: 'normal' });
  const [stats, setStats] = useState(null);
  const [history, setHistory] = useState([]);

  const [tab, setTab] = useState('dispatch');
  const [tool, setTool] = useState(null);            // add-ambulance | add-hospital | closure | congestion
  const [toolOpts, setToolOpts] = useState({ kind: 'ALS', factor: 3 });
  const [patient, setPatient] = useState(null);      // {lat, lon, address}
  const [form, setForm] = useState(EMPTY_FORM);
  const [decision, setDecision] = useState(null);
  const [lastPlanReq, setLastPlanReq] = useState(null);
  const [showSearch, setShowSearch] = useState(true);
  const [showRoads, setShowRoads] = useState(false);
  const [focus, setFocus] = useState(null);
  const [busy, setBusy] = useState(null);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);

  const say = useCallback((msg, ms = 3200) => {
    setToast(msg);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), ms);
  }, []);

  const ready = status?.status === 'ready';

  /* ---------------------------------------------------------- loading */
  useEffect(() => {
    // keep asking until the server is ready (first start downloads the map; errors are retried)
    let stop = false;
    let timer;
    const poll = async () => {
      let next = 1500;
      try {
        const s = await api.status();
        if (stop) return;
        setStatus(s);
        if (s.status === 'ready') {
          api.meta().then(setMeta).catch(() => {});
          return;
        }
        if (s.status === 'error') next = 8000;
      } catch {
        if (!stop) setStatus((p) => (p?.status === 'error' ? p : { status: 'offline', log: [] }));
        next = 4000;
      }
      timer = setTimeout(poll, next);
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, []);

  const refreshFleet = useCallback(() => api.ambulances().then(setAmbulances), []);
  const refreshHospitals = useCallback(() => api.hospitals().then(setHospitals), []);
  const refreshStats = useCallback(() => api.stats().then(setStats).catch(() => {}), []);
  const refreshHistory = useCallback(() => api.dispatches().then(setHistory), []);
  const refreshRoads = useCallback(() => api.roadConditions().then((r) => setRoadState((p) => ({ ...p, ...r }))), []);

  useEffect(() => {
    if (!ready) return;
    refreshFleet(); refreshHospitals(); refreshStats(); refreshHistory(); refreshRoads();
    if (!meta) api.meta().then(setMeta);
    setFocus({ bounds: status.network.bounds });
  }, [ready]); // eslint-disable-line react-hooks/exhaustive-deps

  /* --------------------------------------------------------- planning */
  const runPlan = useCallback(async (req) => {
    setBusy('Running Dijkstra: choosing the ambulance and hospital…');
    try {
      const res = await api.plan(req);
      setDecision(toDecision(res));
      setLastPlanReq({ ...req, dispatch_id: res.dispatch_id });
      const pts = [...res.ambulance.route, ...res.hospital.route];
      if (pts.length) setFocus({ points: pts });
      refreshHistory();
      return res;
    } catch (e) {
      say('⚠ ' + e.message, 6000);
      return null;
    } finally {
      setBusy(null);
    }
  }, [refreshHistory, say]);

  const findDispatch = () => {
    if (!patient) return say('Choose the patient location first (search or click the map)');
    if (!form.emergency_type) return say('Choose the emergency type');
    runPlan({ ...form, lat: patient.lat, lon: patient.lon, address: patient.address, show_search: showSearch });
  };

  // re-plan the current (not yet dispatched) case, e.g. after a road is closed
  const replanIfOpen = useCallback(async () => {
    if (decision && decision.status === 'planned' && lastPlanReq) {
      const res = await runPlan({ ...lastPlanReq, dispatch_id: decision.dispatchId, show_search: showSearch });
      if (res) say('Route re-calculated with the new road conditions');
    }
  }, [decision, lastPlanReq, runPlan, say, showSearch]);

  const act = async (fn, okMsg) => {
    try {
      const d = await fn(decision.dispatchId);
      setDecision((p) => ({ ...p, status: d.status }));
      await Promise.all([refreshFleet(), refreshHospitals(), refreshStats(), refreshHistory()]);
      say(okMsg);
    } catch (e) { say('⚠ ' + e.message, 6000); }
  };

  const newCase = () => {
    setDecision(null); setPatient(null); setForm(EMPTY_FORM); setLastPlanReq(null); setTab('dispatch');
  };

  const openHistory = async (id) => {
    try {
      const d = await api.dispatch(id);
      setDecision(toDecision(d, true));
      setLastPlanReq(null);
      setPatient({ lat: d.emergency.lat, lon: d.emergency.lon, address: d.emergency.address });
      const pts = [...(d.route_to_patient || []), ...(d.route_to_hospital || [])];
      if (pts.length) setFocus({ points: pts });
    } catch (e) { say('⚠ ' + e.message); }
  };

  /* --------------------------------------------------------- map clicks */
  const onMapClick = async (lat, lon) => {
    if (!ready) return;
    try {
      if (tool === 'add-ambulance') {
        const a = await api.addAmbulance({ lat, lon, kind: toolOpts.kind });
        await refreshFleet(); refreshStats();
        say(`${a.code} (${a.kind}) added`);
      } else if (tool === 'add-hospital') {
        const h = await api.addHospital({ lat, lon, name: `New hospital ${hospitals.length + 1}`, facilities: ['emergency'] });
        await refreshHospitals();
        say(`${h.name} added - set its facilities in the list`);
      } else if (tool === 'closure' || tool === 'congestion') {
        const r = await api.addCondition({ kind: tool, lat, lon, factor: toolOpts.factor });
        setRoadState((p) => ({ ...p, ...r }));
        say(tool === 'closure' ? `Road closed (${r.effect.closed_edges} road segments)` :
          `Congestion zone added (${r.effect.congested_edges} road segments slower)`);
        await replanIfOpen();
      } else if (tab === 'dispatch') {
        if (decision) { setDecision(null); setLastPlanReq(null); }
        setPatient({ lat, lon, address: null });
        api.reverse(lat, lon).then((r) => setPatient((p) => (p && p.lat === lat && p.lon === lon ? { ...p, address: r.address } : p)));
      }
    } catch (e) { say('⚠ ' + e.message, 6000); }
  };

  const changeTab = (t) => {
    setTab(t);
    setTool(null);
    if (t === 'history') refreshHistory();
  };

  /* --------------------------------------------------------------- render */
  const net = status?.network;
  const free = ambulances.filter((a) => a.status === 'available').length;

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="logo"><svg width="16" height="16" viewBox="0 0 32 32"><path d="M12 4h8v8h8v8h-8v8h-8v-8H4v-8h8z" fill="white" /></svg></span>
          Ambulance Route Optimizer
        </div>
        <div className="header-stats">
          {status?.area && <span>{status.area.name} · {status.area.size_km} × {status.area.size_km} km</span>}
          {net && <span>{fmtInt(net.intersections)} intersections</span>}
          {ready && <span><b>{free}</b>/{ambulances.length} ambulances free</span>}
          {stats && <span><b>{stats.active_cases}</b> active cases</span>}
          {status?.db_mode === 'sqlite-temporary' && (
            <span className="pill wait" title="No DATABASE_URL: data is kept only until the server restarts. Connect a Neon database in Vercel → Storage.">Demo mode</span>
          )}
          <span className={`pill ${ready ? 'ok' : status?.status === 'error' ? 'bad' : 'wait'}`}>
            {ready ? 'Road network ready' : status?.status === 'error' ? 'Error' : 'Loading…'}
          </span>
        </div>
      </header>

      <div className="layout">
        <aside>
          <nav className="tabs">
            {TABS.map(([k, label]) => (
              <button key={k} className={tab === k ? 'active' : ''} onClick={() => changeTab(k)}>{label}</button>
            ))}
          </nav>
          <div className="panel">
            {tab === 'dispatch' && (
              <DispatchPanel meta={meta} patient={patient} setPatient={setPatient} form={form} setForm={setForm}
                decision={decision} onFind={findDispatch} ready={ready} setFocus={setFocus}
                showSearch={showSearch} setShowSearch={setShowSearch} trafficLabel={meta?.traffic_profiles?.[roadState.traffic_profile]}
                onConfirm={() => act(api.confirm, 'Ambulance dispatched')}
                onComplete={() => act(api.complete, 'Case completed - ambulance is free again at the hospital')}
                onCancel={() => act(api.cancel, 'Dispatch cancelled')}
                onNew={newCase} say={say} />
            )}
            {tab === 'fleet' && (
              <FleetPanel ambulances={ambulances} refresh={() => { refreshFleet(); refreshStats(); }} tool={tool} setTool={setTool}
                toolOpts={toolOpts} setToolOpts={setToolOpts} setFocus={setFocus} say={say} />
            )}
            {tab === 'hospitals' && (
              <HospitalsPanel hospitals={hospitals} meta={meta} refresh={refreshHospitals} tool={tool} setTool={setTool}
                setFocus={setFocus} say={say} />
            )}
            {tab === 'roads' && (
              <RoadsPanel meta={meta} roadState={roadState} setRoadState={setRoadState} tool={tool} setTool={setTool}
                toolOpts={toolOpts} setToolOpts={setToolOpts} setFocus={setFocus} say={say} replan={replanIfOpen}
                showRoads={showRoads} setShowRoads={setShowRoads} />
            )}
            {tab === 'history' && (
              <HistoryPanel history={history} open={(id) => { openHistory(id); setTab('dispatch'); }} />
            )}
          </div>
          <footer className="muted">Map data © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors</footer>
        </aside>

        <main>
          <MapView status={status} ambulances={ambulances} hospitals={hospitals} patient={patient}
            decision={decision} roadState={roadState} focus={focus} onMapClick={onMapClick}
            tool={tool} tab={tab} showSearch={showSearch} showRoads={showRoads} say={say} />
          {toast && <div className="toast">{toast}</div>}
          {busy && <div className="busy"><div className="spinner" /><div>{busy}</div></div>}
          {!ready && (
            <div className="busy">
              <div className="spinner" />
              <div className="load-log">
                <b>{status?.status === 'error' ? 'Could not prepare the road network' :
                  status?.status === 'offline' ? 'Starting the server…' : 'Preparing the road network…'}</b>
                {status?.status !== 'error' && <div>The very first start downloads the map from OpenStreetMap (about 30 seconds).</div>}
                {(status?.log || []).slice(-6).map((l, i) => <div key={i}>{l}</div>)}
                {status?.error && <div className="err">{status.error}<br />Retrying automatically…</div>}
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
