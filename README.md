# Ambulance Route Optimizer

**An Algorithm-Based Emergency Dispatch and Route Planning System** (ADA project)

An emergency operator enters a **patient location** (by address search or by
clicking the map) and an **emergency type**. The system uses the **real road
network of Central Delhi** (50 km × 50 km, from OpenStreetMap) and **Dijkstra's
algorithm** to decide:

1. **which ambulance** should respond
2. **which route** it should take to the patient
3. **which hospital** is suitable for this emergency
4. **which route** to take to that hospital
5. the **estimated total travel time**

It does not call anyone. It gives the optimised decision a control room would
act on.

| Layer | Technology |
|---|---|
| Frontend | React.js + React-Leaflet (Leaflet.js), OpenStreetMap tiles |
| Geocoding | Nominatim API (address ↔ latitude/longitude) |
| Backend | Flask (Python) REST API |
| Database | PostgreSQL (SQLAlchemy) |
| Road data | OpenStreetMap via OSMnx, converted into a weighted graph |
| Algorithm | Dijkstra's algorithm, written from scratch with a binary heap |

---

## 1. Setup

You need **Python 3.10+**, **Node.js 18+** and **PostgreSQL**.

### a) Database

With Docker:

```bash
docker compose up -d
```

This starts PostgreSQL with database `aro`, user `aro`, password `aro`.

Without Docker, install PostgreSQL and run this in `psql`:

```sql
CREATE USER aro WITH PASSWORD 'aro';
CREATE DATABASE aro OWNER aro;
```

### b) Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/Mac: source .venv/bin/activate)
pip install -r requirements-dev.txt
copy .env.example .env          # optional  (Linux/Mac: cp) - change area, DB URL, ...
python run.py
```

On the **first start** the backend:

* creates the PostgreSQL tables
* downloads the drivable road network of Central Delhi with **OSMnx**. The
  50 × 50 km area is big: expect **10–30 minutes and several GB of RAM** the
  first time. The result is cached in `backend/data/cache/`, and later starts
  take seconds. **Tip:** while developing, set `AREA_SIZE_KM=15` in `.env`.
* fetches the **real hospitals** in the area from OpenStreetMap
* creates the **simulated ambulance fleet**

The web page shows the loading progress.

### c) Frontend

```bash
cd frontend
npm install
npm run dev          # open http://localhost:5173
```

Alternatively, a production build is already included in `frontend/dist`, and
Flask serves it at **http://localhost:5000** (`npm run build` refreshes it).

### Offline demo / tests

`backend/sample_data/sample_town.osm` is a small real OpenStreetMap extract.
Set `ROAD_GRAPH_OSM_FILE=sample_data/sample_town.osm` to run without
downloading Delhi.

Tests (they need a database called `aro_test`, owned by `aro`):

```bash
cd backend
python -m pytest -q        # 29 tests
```

---

### Deploying online (Vercel)

See **[DEPLOY_VERCEL.md](DEPLOY_VERCEL.md)**. In short: the frontend and the
Flask backend become two Vercel projects, the database is Neon PostgreSQL from
the Vercel Marketplace, and the road graph is built on your computer
(`python manage.py export-graph`) and deployed as a file.

---

## 2. Using the app

| Tab | What you do |
|---|---|
| **Dispatch** | ① search an address or click the patient on the map → ② pick the emergency type and severity → **Find optimal dispatch**. The map shows the chosen ambulance, the blue route to the patient, the chosen hospital and the red route to the hospital, plus the area Dijkstra searched. The panel answers the 5 questions and explains *why*: every ambulance compared, every suitable hospital, and closer hospitals that were rejected (e.g. "no burn unit"). **Confirm & dispatch** marks the ambulance busy and reserves a bed. **Patient delivered** frees the ambulance at the hospital. |
| **Ambulances** | The fleet with its status (available / busy / maintenance). Add ambulances by clicking the map. |
| **Hospitals** | Real OSM hospitals. Edit facilities and free beds. |
| **Roads** | Traffic level (free flow / normal / peak hour). **Close a road** or add a **traffic jam** by clicking the map. If a case is still being planned, it is re-routed automatically. |
| **History** | Every case is stored in PostgreSQL. Click one to see it on the map again. |

---

## 3. Project pipeline (as in the project document)

```
operator: address / map click ──Nominatim──▶ lat, lon
          emergency type, severity
                │
                ▼
   ┌───────── Flask /api/plan ─────────────────────────────────────────┐
   │ 1. snap patient to the nearest road intersection (grid index)     │
   │ 2. Step A - ambulance:  Dijkstra from each available ambulance    │
   │    (ALS only for critical cases) to the patient → min travel time │
   │ 3. Step B - hospital:   filter by facilities + free beds,         │
   │    one Dijkstra from the patient → fastest suitable hospital      │
   │ 4. total = time(ambulance→patient) + time(patient→hospital)       │
   │ 5. store emergency + dispatch (+ both routes) in PostgreSQL       │
   └───────────────────────────────────────────────────────────────────┘
                │
                ▼
   React-Leaflet map: routes, markers, decision panel
```

### The road network as a weighted graph (`aro/road_network.py`)

* **Vertices** are road intersections. OSMnx simplifies the graph so that only
  real junctions remain.
* **Edges** are road segments, directed: a one-way street is a single edge.
* **Edge weight** is the travel time in seconds:
  `length ÷ speed limit × traffic factor × congestion factor`. A closed road
  has weight ∞.
  * Speed limits come from OSM `maxspeed`, or default Indian city speeds by
    road type.
  * Traffic factors: main roads slow down more than side streets at peak hour,
    so the best route really changes.
* Only the **largest strongly connected component** is kept, so every
  intersection can reach every other.

### Dijkstra's algorithm (`aro/dijkstra.py`)

```
dist[source] = 0; push (0, source) on a min-heap
while heap not empty:
    (d, u) = pop min                    # u is now settled: d is its shortest time
    if u already settled: continue      # outdated heap entry (lazy deletion)
    if d > cutoff: stop                 # nothing further can beat the best answer
    if u is a target: record it; stop when enough targets are found
    for each edge (u → v, w):
        if d + w < dist[v]: dist[v] = d + w; parent[v] = u; push (d + w, v)
```

Time **O((V + E) log V)**, space **O(V)**. Routes are rebuilt by following the
`parent` pointers back from the target.

### Step A: choosing the ambulance

Following the project document, Dijkstra is run **from each available
ambulance to the patient**, and the one with the minimum time wins. Two exact
optimisations make this fast on a 50 km city without ever changing the answer:

* **Lower bound pruning.** No ambulance can arrive faster than
  `straight-line distance ÷ top speed`. Ambulances are tried from the smallest
  bound upwards. Once an ambulance's bound is already worse than the best time
  found, it (and every one after it) is skipped without running Dijkstra.
* **Cutoff.** Each Dijkstra search stops as soon as its smallest tentative time
  is worse than the current best.

On a Delhi-sized test network (176,000 intersections, 30 ambulances, 300
hospitals), a full decision took **180 ms on average**, with typically 2–6
Dijkstra runs instead of 30. The tests compare every decision against brute
force (a full Dijkstra from every ambulance) and confirm it is always the
optimum.

### Step B: choosing the hospital

* Each **emergency type** needs certain facilities: an accident needs trauma,
  a heart attack needs cardiac care, a burn needs a burn unit, a stroke needs
  neuro, and so on. The hospital also needs free beds.
* **Critical** cases, and accident, heart, burn, stroke and breathing cases,
  need an **ALS** (advanced life support) ambulance.
* One Dijkstra from the patient settles intersections in order of travel time,
  so hospitals are reached **fastest first**. It stops after the 5 fastest
  suitable ones.
* Hospitals that are closer but unsuitable are reported with the reason. This
  shows why *the nearest hospital is not always the right one*.

### Database (PostgreSQL)

| Table | Contents |
|---|---|
| `hospitals` | Real OSM hospitals, with facilities and free beds (simulated, editable) |
| `ambulances` | Simulated fleet: code, ALS/BLS, position, status |
| `emergency_types` | Each type and the facilities and ambulance type it needs |
| `emergencies` | Every reported case: patient, address, location, type, severity |
| `dispatches` | The decision: ambulance, hospital, both routes, times, distances, candidate tables, algorithm statistics, status (planned → dispatched → completed / cancelled) |
| `road_conditions` | Road closures and congestion zones entered by the operator |

### REST API (Flask)

`GET /api/status` · `GET /api/meta` · `GET /api/geocode?q=` · `GET /api/reverse` ·
`POST /api/plan` · `POST /api/dispatches/<id>/confirm|complete|cancel` ·
`GET /api/dispatches` · `GET|POST|PATCH /api/ambulances` ·
`GET|POST|PATCH /api/hospitals` · `GET|POST|DELETE /api/road-conditions` ·
`POST /api/traffic-profile` · `GET /api/roads` · `GET /api/stats`

---

## 4. What is real and what is simulated

| Real | Simulated (semi-realistic version) |
|---|---|
| Road network, one-way streets, speed limits (OpenStreetMap) | Ambulance fleet: positions, ALS/BLS, status |
| Hospital names and locations (OpenStreetMap) | Hospital facilities and free beds (deterministic, editable) |
| Address search (Nominatim) | Traffic levels (factors per road class) |

If OpenStreetMap is unreachable while seeding, a short built-in list of major
Delhi hospitals (approximate positions) is used instead.

Routing uses our own Dijkstra on the OSM graph. OSRM, mentioned in the
document as an option, is not needed.

## 5. Project structure

```
backend/
  run.py                start the API server (local)
  app.py                entry point for Vercel
  manage.py             init-db | download | seed | reset | stats | export-graph
  requirements.txt      runtime packages (installed by Vercel)
  requirements-dev.txt  + OSMnx and pytest (for local use)
  aro/
    config.py           settings (.env)
    db.py, models.py    PostgreSQL tables (SQLAlchemy)
    road_network.py     OSMnx download → weighted graph, traffic, closures
    dijkstra.py         Dijkstra's algorithm (+ Bellman-Ford for tests)
    planner.py          the dispatch decision (Steps A and B)
    seed.py             emergency types, OSM hospitals, simulated fleet
    geocoding.py        Nominatim search / reverse (rate-limited)
    api.py, app.py      Flask REST API
  tests/                29 pytest tests (algorithms + API on PostgreSQL)
frontend/
  src/App.jsx           state, map clicks
  src/components/       MapView, DispatchPanel, FleetPanel, HospitalsPanel, RoadsPanel, HistoryPanel
docker-compose.yml      PostgreSQL
DEPLOY_VERCEL.md        step-by-step online deployment
```

Map data © OpenStreetMap contributors (ODbL).
