// Thin wrapper around the Flask REST API.

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch('/api' + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

export const api = {
  status: () => request('GET', '/status'),
  meta: () => request('GET', '/meta'),
  stats: () => request('GET', '/stats'),
  geocode: (q) => request('GET', '/geocode?q=' + encodeURIComponent(q)),
  reverse: (lat, lon) => request('GET', `/reverse?lat=${lat}&lon=${lon}`),

  ambulances: () => request('GET', '/ambulances'),
  addAmbulance: (a) => request('POST', '/ambulances', a),
  updateAmbulance: (id, patch) => request('PATCH', `/ambulances/${id}`, patch),

  hospitals: () => request('GET', '/hospitals'),
  addHospital: (h) => request('POST', '/hospitals', h),
  updateHospital: (id, patch) => request('PATCH', `/hospitals/${id}`, patch),

  plan: (req) => request('POST', '/plan', req),
  confirm: (id) => request('POST', `/dispatches/${id}/confirm`),
  complete: (id) => request('POST', `/dispatches/${id}/complete`),
  cancel: (id) => request('POST', `/dispatches/${id}/cancel`),
  dispatches: () => request('GET', '/dispatches?limit=100'),
  dispatch: (id) => request('GET', `/dispatches/${id}`),

  roadConditions: () => request('GET', '/road-conditions'),
  addCondition: (c) => request('POST', '/road-conditions', c),
  deleteCondition: (id) => request('DELETE', `/road-conditions/${id}`),
  setTraffic: (profile) => request('POST', '/traffic-profile', { profile }),
  roads: (b) => request('GET', `/roads?s=${b.s}&w=${b.w}&n=${b.n}&e=${b.e}`),
};
