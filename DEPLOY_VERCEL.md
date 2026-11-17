# Deploying to Vercel

This project is **one Vercel project**. There's no Root Directory to set, no
second project, and nothing to run on your computer first. On its first start
the server downloads the 5 × 5 km road map and the hospitals of Central Delhi
from OpenStreetMap and stores them in the database.

```
your-repo/            <- repository root = Vercel project root
  app.py              Flask app (Vercel finds it automatically)
  public/             the built React app (served by Vercel's CDN)
  aro/                backend code
  vercel.json         says "this is a Flask app", allows 120 s for the first start
```

## Starting fresh (recommended if you tried the earlier version)

The old Vercel project still has **Root Directory = `backend`**, and its
database may contain the old 50 km data. The simplest fix is to start clean:

1. Vercel: open the old project → **Settings** → scroll to the bottom of the
   General (or Advanced) page → **Delete Project**.
2. In GitHub, create a **new repository** (e.g. `ambulance-route-optimizer`).
   Alternatively, delete everything in the old repository first: the files of
   this new version must be at the **top level** of the repository, not inside
   a sub-folder.

## Step 1: Put the project on GitHub

Unzip the project, open a terminal **inside the unzipped
`ambulance-route-optimizer` folder** (the one that contains `app.py`), and run:

```bash
git init
git add .
git commit -m "Ambulance Route Optimizer"
git branch -M main
git remote add origin https://github.com/<you>/ambulance-route-optimizer.git
git push -u origin main
```

On GitHub, check that `app.py`, `vercel.json` and the `public` folder are at
the top level of the repository.

## Step 2: Create the Vercel project

1. vercel.com → **Add New… → Project** → **Import** your repository.
2. Leave everything as it is: **Root Directory empty** (`./`). The framework is
   set to Flask by `vercel.json`.
3. Click **Deploy**.

The deployment works even before a database is connected. The app then runs in
**Demo mode**: a yellow "Demo mode" label in the header means data resets
whenever Vercel restarts the server.

## Step 3: Connect the PostgreSQL database (Neon)

1. In the project: **Storage** tab → **Create Database** → **Neon (Serverless
   Postgres)** → **Continue**.
2. Region: **Singapore (ap-southeast-1)**; plan: **Free** → **Create**.
3. Connect it to the project, keeping the environment variable prefix at its
   default, so that the variable is called **`DATABASE_URL`**.
4. **Deployments** → **⋯** on the latest deployment → **Redeploy**. Environment
   variables only reach new deployments.

## Step 4: Open the app

Open the project URL (e.g. `https://ambulance-route-optimizer.vercel.app`).

* **First time:** "Preparing the road network…" for about 10–40 seconds while
  the server downloads the map from OpenStreetMap, stores it in Neon, and
  creates the hospitals and ambulances.
* **After that:** a cold start takes a couple of seconds, because the map comes
  from the database.

That's all. Every `git push` redeploys automatically.

---

## If something is not right

The app never shows a blank "500" page for setup problems. The loading screen,
and `https://<your-app>.vercel.app/api/status`, tell you what is wrong, and it
retries by itself.

| Message | What to do |
|---|---|
| "Demo mode" label in the header | Step 3: connect Neon, then Redeploy |
| `Could not download OpenStreetMap data: server busy` | The public OpenStreetMap server is busy. The app retries automatically; wait a minute and reload |
| `Cannot connect to the database … Check DATABASE_URL` | **Settings → Environment Variables**: `DATABASE_URL` must be the Neon connection string, ending in `?sslmode=require` |
| Build error `FLASK_ENTRYPOINT_NOT_FOUND` | **Settings → Build and Deployment** (or General) **→ Root Directory** must be **empty**, and `app.py` must be at the top of the repository |
| Old hospitals or data from the previous version | Use a new Neon database, or locally run `python manage.py reset` with `DATABASE_URL` set to Neon |

Logs: Vercel project → **Logs** tab.

## Changing the area

Add environment variables in **Settings → Environment Variables**, e.g.
`AREA_CENTER=28.5672,77.2100` (AIIMS) and/or `AREA_SIZE_KM=8`, then Redeploy.
The map for the new area is downloaded on the next start. Hospitals and
ambulances are created only when those tables are empty, so for a different
area also reset the data (`python manage.py reset`, see README).
