import { fmtClock, fmtTime, STATUS_LABEL } from '../format.js';

export default function HistoryPanel({ history, open }) {
  return (
    <section>
      <h3>Dispatch history ({history.length})</h3>
      {history.length === 0 && <p className="muted small">No cases yet.</p>}
      <ul className="list">
        {history.map((d) => (
          <li key={d.id} onClick={() => open(d.id)} className="click">
            <div className="grow">
              <div className="between">
                <b>#{d.emergency?.id} {d.emergency?.emergency_label}</b>
                <span className={`pill status-${d.status}`}>{STATUS_LABEL[d.status]}</span>
              </div>
              <div className="small">{d.ambulance?.code} → {d.hospital?.name}</div>
              <div className="muted small">{fmtClock(d.created_at)} · total {fmtTime(d.total_eta_s)}
                {d.emergency?.address ? ` · ${d.emergency.address.slice(0, 50)}` : ''}</div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
