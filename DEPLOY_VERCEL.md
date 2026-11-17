# Deploying to Vercel

The app is deployed as **two Vercel projects from the same GitHub repository**,
plus a **Neon PostgreSQL** database that you add from Vercel's Marketplace:

```
browser ──▶ frontend project (React, static)        https://aro.vercel.app
               │  /api/*  is forwarded (rewrite) to ▼
            backend project (Flask = 1 Vercel Function) https://aro-api.vercel.app
               │
               ├── backend/deploy/roadnet.pkl.gz   road graph, built on YOUR computer
               └── Neon PostgreSQL                  hospitals, ambulances, cases, closures
```

**Why the road graph is built on your computer:** downloading 50 × 50 km of
OpenStreetMap with OSMnx needs several GB of memory and many minutes. That
doesn't fit in a Vercel Function. You build it once locally. The finished file
for all of Central Delhi is only a few tens of MB, and the backend loads it in
a couple of seconds.

The same goes for **seeding**: fetching the hospitals from OpenStreetMap also
needs OSMnx, so you run `manage.py seed` on your computer, writing straight
into the Neon database.

---

## Step 0 - Before you start

* The project runs locally (see README).
* You have a **GitHub** account and a **Vercel** account. Sign in to Vercel
  with GitHub.

## Step 1 - Put the project on GitHub

Create an empty repository on GitHub (e.g. `ambulance-route-optimizer`). Then,
in the project folder:

```bash
git init
git add .
git commit -m "Ambulance Route Optimizer"
git branch -M main
git remote add origin https://github.com/<you>/ambulance-route-optimizer.git
git push -u origin main
```

(`.gitignore` already leaves out `node_modules`, `.env`, `.venv` and the
local cache.)

## Step 2 - Create the backend project + database

1. On vercel.com: **Add New… → Project**, then import your repository.
2. **Root Directory:** click *Edit* and choose **`backend`**. Vercel detects
   Flask from `requirements.txt` and `app.py`.
3. Click **Deploy**. The first deployment works, but its status page will say
   the database and road graph are missing. That's expected.
4. In the new project, open the **Storage** tab, choose **Neon (Serverless
   Postgres)**, then **Create / Connect**. Pick a region close to India (e.g.
   Singapore / `ap-southeast-1`) and the free plan.
   Vercel then adds **`DATABASE_URL`** to the project's environment variables
   automatically.
5. **Settings → Environment Variables:** add these three, with the same values
   you used locally:

   | Name | Value |
   |---|---|
   | `AREA_NAME` | `Central Delhi` |
   | `AREA_CENTER` | `28.6315,77.2167` |
   | `AREA_SIZE_KM` | `50` |

6. Note the backend's address, e.g. `https://aro-api.vercel.app`
   (Project → **Domains**).

## Step 3 - Fill the database and build the road graph (on your computer)

1. In Vercel, open **Storage → your Neon database → `.env.local` tab** (or
   **Settings → Environment Variables**) and copy the value of
   **`DATABASE_URL`**. It looks like
   `postgresql://user:pass@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
2. Put it in **`backend/.env`**, together with the same area settings:

   ```
   DATABASE_URL=postgresql://user:pass@ep-xxxx-pooler....neon.tech/neondb?sslmode=require
   AREA_NAME=Central Delhi
   AREA_CENTER=28.6315,77.2167
   AREA_SIZE_KM=50
   ```

3. Run these in `backend/` with your virtual environment active:

   ```bash
   pip install -r requirements-dev.txt
   python manage.py seed           # tables + real OSM hospitals + simulated ambulances -> Neon
   python manage.py export-graph   # writes backend/deploy/roadnet.pkl.gz
   ```

   `export-graph` reuses the road network you already downloaded locally (it's
   cached), so it's quick. It prints the file size. The file must stay under
   **100 MB**, because GitHub rejects bigger files.

4. Commit the graph file and push:

   ```bash
   git add backend/deploy/roadnet.pkl.gz
   git commit -m "Add pre-built road graph"
   git push
   ```

   Vercel redeploys the backend automatically.

5. Check it by opening `https://<your-backend>.vercel.app/api/status`. After a
   few seconds it should say `"status": "ready"`.

> **Important:** after this, put your local `DATABASE_URL` back to your local
> database. Otherwise local experiments change the live data. Running
> `python manage.py reset` against Neon would delete everything.

## Step 4 - Create the frontend project

1. Edit **`frontend/vercel.json`** and replace `YOUR-BACKEND` with your
   backend's address from Step 2:

   ```json
   { "source": "/api/:path*", "destination": "https://aro-api.vercel.app/api/:path*" }
   ```

   Commit and push.
2. On vercel.com: **Add New… → Project**, then import the **same** repository
   again.
3. **Root Directory:** `frontend`. The framework preset is **Vite**, the build
   command is `npm run build` and the output is `dist` (all detected
   automatically).
4. Click **Deploy**, then open the URL you get (e.g. `https://aro.vercel.app`).
   That's the app.

The rewrite in `vercel.json` makes the browser talk only to the frontend
address. Vercel forwards `/api/...` to the backend, so no CORS setup is needed.

---

## How it behaves on Vercel

* **First request after a quiet period** (cold start): the backend loads the
  road graph, which takes a couple of seconds. The page shows "Preparing the
  road network…" meanwhile.
* **Several backend copies can run at once.** The traffic level and road
  closures are stored in PostgreSQL, and every copy re-applies them before
  planning. So a closure entered by one operator is used by all copies.
* **Limits:** the Vercel free (Hobby) plan gives a function up to 2 GB of
  memory, and Delhi needs well under that. The Neon free plan's storage is
  plenty for thousands of cases.
* **Logs:** Vercel → backend project → **Logs** shows Python errors.

## Updating later

* Code changes: `git push`, and both projects redeploy.
* A different area (`AREA_*`): change it locally and in the backend's
  environment variables, run `python manage.py download`, then `export-graph`,
  push, and redeploy. If the hospitals should change too, run
  `python manage.py reset` against Neon.

## Troubleshooting

| Status page / error says | Fix |
|---|---|
| `Road graph file not found` | Step 3.3–3.4: run `export-graph` and commit `backend/deploy/roadnet.pkl.gz` |
| `The database has no hospitals/ambulances yet` | Step 3: run `python manage.py seed` with `DATABASE_URL` pointing at Neon |
| `connection ... failed` / SSL errors | Check `DATABASE_URL` and keep `?sslmode=require` at the end |
| Frontend shows "Waiting for the backend" | `frontend/vercel.json` still has `YOUR-BACKEND`, or the address is wrong |
| Address search fails | Nominatim allows 1 request per second. Wait a moment, or click the map instead |
