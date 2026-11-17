export const fmtTime = (s) => {
  if (s == null) return '–';
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  if (m < 60) return `${m} min ${sec} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
};

export const fmtDist = (m) => (m == null ? '–' : m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`);

export const fmtInt = (n) => (n == null ? '–' : Number(n).toLocaleString('en-IN'));

export const fmtClock = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
};

export const STATUS_LABEL = {
  planned: 'Planned', dispatched: 'Dispatched', completed: 'Completed', cancelled: 'Cancelled',
  available: 'Available', busy: 'Busy', maintenance: 'Maintenance',
};
