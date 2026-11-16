# Deploying to Railway (Phase 1, step 9)

These steps need Wisdom's own GitHub and Railway accounts. Railway's UI labels change
now and then; if a name below doesn't match, look for the nearest equivalent.

## 1. Push the repo to a private GitHub repo

```powershell
gh auth login
cd C:\Users\Godswisdom\mycar
gh repo create autocare --private --source . --push
```

## 2. Create the Railway project (EU region)

1. railway.com → **New Project** → **Deploy PostgreSQL**. In the project settings,
   choose an **EU West** region (closest to Nigeria).
2. **+ New** → **GitHub Repo** → `autocare`. Rename the service to `api`.
   - Settings → **Root Directory**: `/backend`
   - Settings → **Config-as-code file path**: `/backend/railway.toml`
   - Settings → Networking → **Generate Domain**
3. `api` → **Variables**:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference variable) |
   | `ENVIRONMENT` | `production` |
   | `JWT_SECRET` | output of the command below |
   | `CORS_ORIGINS` | `http://localhost:3000` for now (the Vercel URL comes in Phase 7) |

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   The app refuses to start in production with the default secret.

4. **+ New** → **GitHub Repo** → `autocare` again. Rename it to `worker`.
   - Root Directory `/backend`, config path `/backend/railway.worker.toml`
   - Same variables as `api` (copy them, or use shared variables).

Each deploy runs `alembic upgrade head` (the pre-deploy command in `railway.toml`)
before the new version takes traffic. A failed migration stops the deploy.

## What actually happened in the first deploy (2026-11-16)

- **Database is on Neon, not Railway** (ADR-005). `DATABASE_URL` is Neon's **direct**
  connection string (pooling off). The app now accepts it as pasted; the hand-edited
  form `postgresql+asyncpg://...?ssl=require` also works.
- **Railway didn't pick up `railway.toml`**, so the builder showed Railpack and no
  pre-deploy step ran. What fixed it:
  - Root Directory `/backend`, so Railway finds the Dockerfile.
  - On `api`: **Settings → Deploy → Add pre-deploy step** = `alembic upgrade head`,
    Healthcheck Path `/health`. On `worker`: start command `python -m app.jobs.worker`,
    no pre-deploy step.
- **A failed deploy leaves the previous version serving.** If `/health/ready` still shows
  an old error after a push, check the newest deployment's status before anything else.
- `/health/ready` saying `migrations: behind (db=None, ...)` means the DB is reachable but
  the pre-deploy migration never ran.
- Admin flag on Neon: Neon console → **SQL Editor** (instead of Railway's Data tab).

Result: request list 33/33 against `https://mycar-api-production.up.railway.app`.

## 3. Exit-gate checks

```powershell
# a) migrations ran and the DB is at head
curl https://<your-api>.up.railway.app/health/ready

# b) the whole request list passes against Railway
cd C:\Users\Godswisdom\mycar\backend
uv run python restfox/phase1_requests.py run https://<your-api>.up.railway.app
```

c) The worker picks up jobs:
1. Register yourself through the API (or Restfox request 03).
2. Railway → Postgres → **Data/Query**:
   `UPDATE users SET is_admin = true WHERE email = 'you@example.com';`
3. Log in, then `POST /admin/jobs/ping` with your token. Note the returned `id`.
4. `GET /admin/jobs/<id>` should show `"status": "succeeded"` within a few seconds,
   and the `worker` logs show `pong`.

Then check the deploy logs for the migration line and record the actual monthly usage
in `docs/cost.md` after a week.
