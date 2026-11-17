# Ambulance Route Optimizer

**An Algorithm-Based Emergency Dispatch and Route Planning System** (ADA project)

An emergency operator enters a **patient location** (address search or a click
on the map) and an **emergency type**. On the **real road network of Central
Delhi** (5 km × 5 km around Connaught Place, from OpenStreetMap), the system
uses **Dijkstra's algorithm** to decide:

1. **which ambulance** should respond
2. **which route** it should take to the patient
3. **which hospital** is suitable for this emergency
4. **which route** to take to that hospital
5. the **estimated total travel time**

| Layer | Technology |
|---|---|
| Frontend | React.js + React-Leaflet (Leaflet.js), OpenStreetMap tiles |
| Geocoding | Nominatim API (address ↔ latitude/longitude) |
| Backend | Flask (Python) REST API |
| Database | PostgreSQL (Neon on Vercel). SQLite is used automatically if no database is configured |
| Road data | OpenStreetMap (Overpass API), converted into a weighted directed graph |
| Algorithm | Dijkstra's algorithm, written from scratch with a binary heap |

**Deploying online:** see **[DEPLOY_VERCEL.md](DEPLOY_VERCEL.md)**. It's one
Vercel project, with nothing to prepare on your computer.

---

## 1. Run it on your computer

You need **Python 3.10+**.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**. The first start downloads the 5 × 5 km road
map from OpenStreetMap, which takes about 10–40 s. After that it's stored in
the database.

* **Database:** without any setup the app uses a local SQLite file
  (`local.db`). To use **PostgreSQL**, start one (e.g. `docker compose up -d`)
  and create a `.env` file containing
  `DATABASE_URL=postgresql://aro:aro@localhost:5432/aro`.
* **Changing the web app:** the React source is in `frontend/`. Run
  `npm install` and then `npm run dev` (http://localhost:5173, with
  `python app.py` running alongside). `npm run build` writes the finished app
  to `public/`; commit `public/` so Vercel serves the new version.
* **Tests:** `pip install -r requirements-dev.txt`, then `python -m pytest -q`
  (36 tests; the PostgreSQL ones need a database called `aro_test`).
* **Maintenance:** `python manage.py stats | refresh-map | reset`.

---

## 2. Using the app

| Tab | What you do |
|---|---|
| **Dispatch** | ① Search an address or click the patient on the map. ② Pick the emergency type and severity. Then click **Find optimal dispatch**. The map shows the chosen ambulance, the blue route to the patient, the chosen hospital, the red route to the hospital, and the area Dijkstra searched. The panel answers the 5 questions and explains *why*: every ambulance compared, every suitable hospital, and closer hospitals that were rejected (e.g. "no burn unit"). **Confirm & dispatch** marks the ambulance busy and reserves a bed. **Patient delivered** frees the ambulance at the hospital. |
| **Ambulances** | The fleet with its status (available / busy / maintenance). Add ambulances by clicking the map. |
| **Hospitals** | Real OSM hospitals. Edit their facilities and free beds. |
| **Roads** | Traffic level (free flow / normal / peak hour). **Close a road** or add a **traffic jam** by clicking the map. A case that is still being planned is re-routed automatically. |
| **History** | Every case is stored in the database. Click one to show it on the map again. |

---

## 3. How it works

```
operator: address / map click ──Nominatim──▶ lat, lon ; emergency type, severity
                │
                ▼
   ┌───────── Flask  POST /api/plan ───────────────────────────────────┐
   │ 1. snap the patient to the nearest open road intersection         │
   │ 2. Step A (ambulance): Dijkstra from each available ambulance     │
   │    (ALS only for critical cases) to the patient → minimum time    │
   │ 3. Step B (hospital): filter by facilities + free beds,           │
   │    one Dijkstra from the patient → fastest suitable hospital      │
   │ 4. total = time(ambulance→patient) + time(patient→hospital)       │
   │ 5. store the emergency + dispatch (+ both routes) in the database │
   └───────────────────────────────────────────────────────────────────┘
                │
                ▼
   React-Leaflet map: routes, markers, decision panel
```

### Road network → weighted graph (`aro/osm.py`, `aro/road_network.py`)

1. **Download:** all drivable roads and all hospitals in the 5 × 5 km square
   are downloaded from the OpenStreetMap **Overpass API**. This is the same
   source and the same "drive" road filter OSMnx uses, implemented directly so
   it runs inside a Vercel Function. The data is stored compressed in the
   database (`osm_cache` table) and is downloaded only once.
2. **Graph:** every road segment becomes a directed edge (one-way streets in
   one direction only). The **weight is the travel time in seconds**:
   `length ÷ speed limit × traffic factor × congestion factor`. A closed road
   gets weight ∞.
   * Speed limits come from OSM `maxspeed`, or default Indian city speeds by
     road type.
3. **Simplification:** chains of points that are not intersections are merged
   into one edge that keeps the road's shape, as OSMnx's `simplify_graph` does.
   Shortest-path distances don't change.
4. **Largest strongly connected component** (Kosaraju's algorithm,
   O(V + E)): every intersection can reach every other.

### Dijkstra's algorithm (`aro/dijkstra.py`)

```
dist[source] = 0; push (0, source) on a min-heap
while heap not empty:
    (d, u) = pop min                    # u settled: d is its shortest time
    if u already settled: continue      # outdated entry (lazy deletion)
    if d > cutoff: stop                 # nothing further can beat the best answer
    if u is a target: record it; stop when enough targets are found
    for each edge (u → v, w):
        if d + w < dist[v]: dist[v] = d + w; parent[v] = (u, edge); push (d + w, v)
```

Time **O((V + E) log V)**, space **O(V)**. Routes are rebuilt by following the
parent pointers back.

### Step A: the ambulance

Dijkstra runs from **each available ambulance** to the patient, and the one
with the minimum time wins. Two exact optimisations never change the answer
(the tests compare the result against brute force):

* **Lower-bound pruning.** No ambulance can arrive faster than
  `straight-line distance ÷ top speed`. Ambulances are tried from the smallest
  bound upwards, and once the bound is worse than the best time found, all
  remaining ambulances are skipped without running Dijkstra.
* **Cutoff.** Each search stops once its smallest tentative time exceeds the
  current best.

### Step B: the hospital

* Each emergency type needs certain facilities: an accident needs trauma, a
  heart attack needs cardiac care, a burn needs a burn unit, a stroke needs
  neuro, and so on. The hospital also needs **free beds**.
* Critical cases, and accident, heart, burn, stroke and breathing cases, need
  an **ALS** (advanced life support) ambulance.
* **One** Dijkstra from the patient reaches hospitals in order of travel time
  and stops after the 5 fastest suitable ones. Closer but unsuitable hospitals
  are listed with the reason. This shows why *the nearest hospital is not
  always the right one*.

### Database tables

| Table | Contents |
|---|---|
| `hospitals` | Real OSM hospitals, with facilities and free beds (simulated, editable) |
| `ambulances` | Simulated fleet: code, ALS/BLS, position, status |
| `emergency_types` | Each type and the facilities and ambulance type it needs |
| `emergencies` | Every reported case: patient, address, location, type, severity |
| `dispatches` | The decision: ambulance, hospital, both routes, times, distances, compared candidates, algorithm statistics, status (planned → dispatched → completed / cancelled) |
| `road_conditions` | Road closures and congestion zones |
| `settings` | Traffic level, shared by all server instances |
| `osm_cache` | The downloaded OpenStreetMap data (compressed) |

### REST API

`GET /api/status` · `GET /api/meta` · `GET /api/geocode?q=` · `GET /api/reverse` ·
`POST /api/plan` · `POST /api/dispatches/<id>/confirm|complete|cancel` ·
`GET /api/dispatches[/<id>]` · `GET|POST|PATCH /api/ambulances` ·
`GET|POST|PATCH /api/hospitals` · `GET|POST|DELETE /api/road-conditions` ·
`POST /api/traffic-profile` · `GET /api/roads` · `GET /api/stats`

## 4. Real vs simulated

| Real | Simulated (semi-realistic version) |
|---|---|
| Road network, one-way streets, speed limits (OpenStreetMap) | Ambulance fleet: positions, ALS/BLS, status |
| Hospital names and locations (OpenStreetMap) | Hospital facilities and free beds (deterministic, editable) |
| Address search (Nominatim) | Traffic levels (factors per road class) |

## 5. Project structure

```
app.py                Flask entry point (Vercel + local: python app.py)
vercel.json           Vercel settings (Flask, 120 s max duration)
requirements.txt      Python packages
aro/
  config.py           settings (environment variables / .env)
  db.py, models.py    database tables (SQLAlchemy)
  osm.py              OpenStreetMap download (Overpass) and .osm parsing
  road_network.py     weighted graph: simplification, SCC, traffic, closures
  dijkstra.py         Dijkstra's algorithm (+ Bellman-Ford for the tests)
  planner.py          the dispatch decision (Steps A and B)
  seed.py             emergency types, hospitals, simulated fleet
  geocoding.py        Nominatim search / reverse (rate-limited)
  server.py, api.py   Flask app, start-up, REST API
public/               built React app (served by Vercel)
frontend/             React source (Vite)
tests/                36 pytest tests
```

Map data © OpenStreetMap contributors (ODbL).
