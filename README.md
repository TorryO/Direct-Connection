# Direct-Connection

This allows the owner of a USB flash drive to send files to it **after handing it to someone else**.
When the drive is plugged into any computer with internet, its agent pulls down whatever the owner
queued. Think of it as "Dropbox for a flash drive".

```
Owner (CLI)                Server (API + disk)              Drive Agent (portable app ON the drive)
  send files  ----------->  per-drive queue, SHA-256  <----  polls over HTTPS, downloads,
                            storage, delivery receipts       verifies hash, saves to Inbox/
```

1. The owner registers a drive and gets a token for it.
2. The owner queues files for that drive with the CLI.
3. The recipient plugs the drive in and starts the agent themselves.
4. The agent downloads anything pending into `Inbox/`, verifies each SHA-256, and reports delivery.

## Status: MVP 0.1

- [x] Server API: register drive, upload file for a drive, list pending files, download file
- [x] Owner CLI to upload files to a drive ID
- [x] Drive Agent: check for new files, download to `Inbox/`, verify hash, show log
- [x] Per-drive token authentication (token stored as a SHA-256 hash, revocable, rotatable)
- [x] Basic file size and type limits
- [x] Delivery receipts (the owner sees when a file arrived)

See [Roadmap](#roadmap) for what is not built yet.

## Trust and safety rules

The agent writes into someone else's session, so it is deliberately boring and visible:

- **No autorun.** The agent runs only when the recipient opens it.
- **Visible at all times.** A window shows a live activity log with Pause, Check now, Open Inbox and Quit.
- **Writes only inside the drive's `Inbox/` folder.** It never touches the host computer's files, settings or startup items.
- **Never executes what it downloads.** Executables and scripts are refused by the server *and* by the agent.
- **Integrity checks.** Every file is hashed with SHA-256: once while downloading, once again after it lands on the drive. A mismatch is discarded and the file stays queued.
- **Revocable.** Each drive has its own token the owner can revoke or rotate at any time.

## Requirements

- Python 3.10+ for the server.
- Python 3.9+ for the CLI and agent (they use only the standard library). The agent window needs tkinter, which ships with the python.org installers on Windows and macOS.

## Getting started

```bash
git clone https://github.com/TorryO/Direct-Connection.git
cd Direct-Connection

python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip install -r requirements.txt

# Pick an admin key and keep it secret.
python -c "import secrets; print(secrets.token_urlsafe(32))"
export DC_ADMIN_KEY=<that value>               # Windows: setx DC_ADMIN_KEY "<that value>"

# Optional: password for the owner dashboard at /dashboard
python -c "import secrets; print(secrets.token_urlsafe(24))"
export DC_DASHBOARD_PASSWORD=<that value>       # Windows: setx DC_DASHBOARD_PASSWORD "<that value>"

.venv/bin/python -m server                     # http://127.0.0.1:8000, data in ./data
```

Server environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DC_ADMIN_KEY` | *(required, 16+ chars)* | Owner key for the CLI and owner endpoints |
| `DC_DASHBOARD_PASSWORD` | *(unset = dashboard disabled)* | Password for the owner web dashboard at `/dashboard` |
| `DC_DATA_DIR` | `./data` | SQLite database and uploaded files when the two settings below are unset |
| `DC_DATABASE_URL` | *(unset = SQLite)* | `postgres://` or `postgresql://` URL; required on Vercel |
| `DC_S3_BUCKET` | *(unset = local disk)* | S3-compatible bucket for file bytes; required on Vercel |
| `DC_S3_ENDPOINT_URL` | *(AWS default)* | e.g. `https://<account>.r2.cloudflarestorage.com` |
| `DC_S3_REGION` | `auto` | Bucket region |
| `DC_S3_ACCESS_KEY_ID` / `DC_S3_SECRET_ACCESS_KEY` | *(required with the bucket)* | Credentials that can presign PUT/GET on the bucket |
| `DC_S3_PREFIX` | *(empty)* | Optional key prefix, e.g. `direct-connection` |
| `DC_HOST` / `DC_PORT` | `127.0.0.1` / `8000` | Listen address. Put it behind HTTPS before exposing it |
| `DC_MAX_FILE_BYTES` | `52428800` (50 MiB) | Per-file upload limit |

## Owner dashboard

Set `DC_DASHBOARD_PASSWORD` and open `http://<host>:8000/dashboard`. Sign in with that password
and you get one row per flash drive:

- **Status** — Online if the drive's agent checked in within the last 90 seconds, otherwise
  Offline (with the time it was last seen). Revoked drives are flagged and always shown offline.
- **Program** — Installed once the CLI provisions the drive or the agent connects for the first
  time, so you can tell which sticks actually carry the program.
- Pending file count, when the drive was added, and a running total of drives, online drives,
  installed drives and queued files.
- **Drive detail**: click a drive name for its files with size, note, queued time and delivery
  receipt, plus a Remove button that deletes the queued file and its stored bytes.
- **Actions** per drive: **Send** queues one or more files with an optional note (same size,
  type and hashing rules as the CLI), **Revoke** disables the drive for good after a confirmation,
  and **Rotate** issues a new token and shows it once for you to update the drive. Revoked
  drives show no actions.

The page refreshes itself every 15 seconds. Sessions are an `HttpOnly` cookie signed with the
admin key and expire after 12 hours; sign out from the header. The same data is available to
tools at `GET /api/dashboard/drives` with either the session cookie or `X-Admin-Key`. Action
requests from the page also send `X-Dashboard-Request: 1`, which stops another site from posting
with your session cookie; scripts using `X-Admin-Key` do not need it.

Put the server behind HTTPS before exposing it, since the password and session cookie travel
with every request.

## Deploy on Vercel

Vercel Functions have an ephemeral filesystem and a 4.5 MB request/response
limit, so this app switches to Postgres plus S3-compatible object storage and
hands out short-lived URLs instead of moving bytes through the function.
`main.py` exports the FastAPI app, `vercel.json` sets a 60s function limit.

1. Create a Postgres database (Neon, Supabase, Vercel's Postgres integration)
   and an S3-compatible bucket (Cloudflare R2, Supabase Storage, AWS S3, MinIO).
2. Allow browser uploads to the bucket from your dashboard origin. For S3/R2:

   ```json
   [{
     "AllowedOrigins": ["https://your-app.vercel.app"],
     "AllowedMethods": ["PUT"],
     "AllowedHeaders": ["content-type"],
     "MaxAgeSeconds": 3000
   }]
   ```

3. Import the repo into Vercel (framework preset: FastAPI) and set:

   | Variable | Value |
   | --- | --- |
   | `DC_ADMIN_KEY` | random, 16+ chars |
   | `DC_DASHBOARD_PASSWORD` | random, 16+ chars |
   | `DC_DATABASE_URL` | your Postgres URL (add `?sslmode=require` if needed) |
   | `DC_S3_BUCKET` / `DC_S3_ACCESS_KEY_ID` / `DC_S3_SECRET_ACCESS_KEY` | bucket details |
   | `DC_S3_ENDPOINT_URL`, `DC_S3_REGION` | for R2/Supabase/MinIO |

4. Deploy with the dashboard (`vercel --prod`) or by pushing a branch. Once the
   deployment is live, open `/dashboard` and sign in.

Nothing needs a build command and no local files are written. The schema is
created on first connection, and uploads (`dc.py send` and the dashboard) go
straight to the bucket, so the 4.5 MB function limit does not apply. The agent
fetches a short-lived download link and saves to `Inbox/` as before.

Notes:

- Vercel's Postgres integration works for `DC_DATABASE_URL`. Vercel Blob is
  not S3-compatible, so use an S3-compatible bucket for the file bytes.
- Presigned links last 15 minutes. If a drive's clock is badly wrong, downloads
  fail until it is corrected.
- `DC_DATA_DIR` is ignored once `DC_DATABASE_URL` and `DC_S3_BUCKET` are set.
- Cold starts create one database round trip and one S3 client; requests after
  that reuse them per instance.

## Owner workflow

Use `DC_SERVER_URL` (default `http://127.0.0.1:8000`) and `DC_ADMIN_KEY` in the same shell.

```bash
# Prepare a drive folder: registers the drive, writes direct-connection.json + agent.py, creates Inbox/
python owner-cli/dc.py provision /media/SAMSTICK --name "Sam's stick"

# Or register a drive without a folder yet
python owner-cli/dc.py create --name "Sam's stick"     # token is printed once

# Queue files (a note shows up in the agent log)
python owner-cli/dc.py send <drive-id> letter.pdf photos/*.jpg --note "Trip photos"

# See what is queued and what has arrived
python owner-cli/dc.py files <drive-id>
python owner-cli/dc.py drives

# Kill or replace a drive's access
python owner-cli/dc.py revoke <drive-id>
python owner-cli/dc.py rotate <drive-id>
```

Only the SHA-256 hash of each drive token is stored, so a token cannot be recovered from the
database. If one is lost, rotate it.

## Recipient workflow

The drive contains `agent.py`, `direct-connection.json` and `Inbox/`.

- Windows/macOS: double-click `agent.py`, or run `python agent.py` (or `py agent.py`) if `.py` files are not associated.
- Linux: `python3 agent.py`

For a one-off check without the window: `python agent.py --once` (logs to the console).

The window shows connection status, every download with its hash result, and errors such as
"offline" (it simply retries every `poll_seconds`, default 30). If the owner revokes the drive,
the agent says so and stops downloading.

## API

Owner endpoints require the `X-Admin-Key` header. Agent endpoints require
`Authorization: Bearer <drive token>`.

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/drives` | owner | Register a drive, returns its token once |
| `GET` | `/api/drives` | owner | List drives with pending counts |
| `GET` | `/api/drives/{id}` | owner | One drive |
| `POST` | `/api/drives/{id}/files` | owner | Upload in one request (`file` part, optional `note`); local/small files |
| `POST` | `/api/drives/{id}/files/upload-url` | owner | Reserve a file, get a presigned upload URL |
| `POST` | `/api/drives/{id}/files/{file_id}/complete` | owner | Confirm the upload with its `sha256` |
| `PUT` / `GET` | `/api/blob/upload/{key}` / `/api/blob/download/{key}` | signed URL | Local-storage upload/download |
| `GET` | `/api/drives/{id}/files` | owner | Queue with `delivered_at` receipts |
| `DELETE` | `/api/drives/{id}/files/{file_id}` | owner | Remove a file and its bytes |
| `POST` | `/api/drives/{id}/installed` | owner | Record that the agent files were written to the drive |
| `POST` | `/api/drives/{id}/revoke` | owner | Permanently disable the drive token |
| `POST` | `/api/drives/{id}/rotate-token` | owner | Issue a new token, invalidating the old one |
| `GET` | `/api/agent/me` | agent | Drive name, used by the first-run screen |
| `GET` | `/api/agent/queue` | agent | Pending files (id, name, size, sha256, note) |
| `GET` | `/api/agent/files/{file_id}/link` | agent | Short-lived download URL plus `sha256` |
| `GET` | `/api/agent/files/{file_id}` | agent | Download; `X-Content-Sha256` header (local disk, or redirect to storage) |
| `POST` | `/api/agent/files/{file_id}/ack` | agent | Confirm delivery with the `sha256` form field |
| `GET` | `/api/health` | anyone | Liveness probe |
| `GET` | `/api/dashboard/drives` | owner | Overview rows with `online` / `installed` state |
| `GET` | `/api/dashboard/drives/{id}` | owner | Drive detail with its files and receipts |
| `DELETE` | `/api/dashboard/drives/{id}/files/{file_id}` | owner | Remove a file and its stored bytes |
| `POST` | `/api/dashboard/drives/{id}/files` | owner | Upload from the dashboard (session) |
| `POST` | `/api/dashboard/drives/{id}/revoke` | owner | Revoke from the dashboard (session) |
| `POST` | `/api/dashboard/drives/{id}/rotate-token` | owner | Rotate from the dashboard (session) |
| `GET` | `/dashboard` | owner | Login page, or the drive overview with a session |
| `GET` | `/dashboard/drives/{id}` | owner | Drive detail page |

Interactive docs are at `/docs` while the server runs.

## Project structure

```
Direct-Connection/
  main.py            # Vercel entrypoint (exports the FastAPI app)
  vercel.json        # function duration
  server/            # FastAPI app, SQLite or Postgres, local disk or S3
    app.py           # routes, auth dependencies and signed-URL endpoints
    db.py            # drives + files tables, SQLite and Postgres backends
    storage.py       # local/S3 storage, sanitizing, limits, hashing
    config.py        # environment-driven settings
    dashboard.py     # login, drive overview and drive detail (HTML + vanilla JS)
  owner-cli/dc.py    # owner tool (stdlib only)
  agent/agent.py     # portable agent for the drive (stdlib + tkinter)
  tests/             # API, upload-flow, storage, Postgres and agent tests
  requirements.txt
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The agent tests start a real uvicorn server and run the agent against it, including corrupted
downloads, blocked file types, name collisions and revocation.

The Postgres backend is tested against a real database when you provide one:

```bash
docker run --rm -d --name dc-pg -p 55432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
DC_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres \
  .venv/bin/python -m pytest tests/test_postgres.py -q
docker rm -f dc-pg
```

## Roadmap

- Owner web dashboard: login, overview, drive detail and per-drive actions are done
- Serverless storage: Postgres and S3 backends are done; retention rules and quotas are not
- Optional end-to-end encryption of files
- Auto-update for the agent
- Windows, macOS and Linux builds of the agent (PyInstaller)
- Retention limits and per-drive quotas

## License

MIT (add a LICENSE file)
