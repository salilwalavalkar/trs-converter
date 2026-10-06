# trs-converter

TRS Converter for SJOS — a small web app for Safe Electric that turns a completed
**Test Record Sheet (TRS)** Excel workbook into a high-resolution JPEG and downloads it
to the user's device.

## What it does

1. The user signs in with a username and password.
2. They choose a `.xlsx` / `.xls` file (tap to choose or drag & drop). The browser checks
   the type and size before uploading.
3. A progress bar tracks the upload, then the server-side conversion:
   1. The upload is validated (extension, size, not empty, real Excel file signature) and
      saved under a sanitised name in a per-job temporary directory.
   2. Headless **LibreOffice** converts the workbook to PDF.
   3. **Poppler** (`pdftoppm`) renders **page 1** to a **300 DPI JPEG**.
4. The JPEG downloads automatically, with a preview and a "Download again" button.

Conversions run one at a time in a background worker (LibreOffice uses a lot of memory) and
the page polls for progress. Results are kept for an hour, and users can only see their own jobs.

## Features

- **Authentication and authorization** — signed session cookies (12 h). Two roles: `user`
  (can convert) and `admin` (can also view analytics). Passwords are stored as PBKDF2-SHA256
  hashes. After 5 failed sign-ins for a username/IP pair, that pair is locked out for 15 minutes.
- **Analytics** — events are stored in SQLite and shown to admins at `/admin/analytics`
  (totals, success rate, conversions per day, per-user activity, recent events).
  Events tracked: sign-ins (succeeded/failed/locked), sign-outs, page views, file selected/rejected,
  uploads received/rejected, conversions succeeded (with duration) or failed (with reason),
  downloads, "convert another".
- **Error handling** — clear messages for wrong file type, empty or oversized files,
  corrupted/renamed files, LibreOffice/Poppler failures and timeouts, expired sessions,
  expired jobs and network errors.

## Project layout

| Path | Purpose |
| --- | --- |
| `main.py` | FastAPI app: routes, sessions, upload validation |
| `auth.py` | User loading, password hashing, login throttling, role checks; `python auth.py hash-password` |
| `jobs.py` | Background conversion jobs and progress tracking |
| `converter.py` | LibreOffice → PDF → JPEG pipeline |
| `analytics.py` | SQLite event store and summary queries |
| `templates/` | `login.html`, `index.html` (converter), `analytics.html` (admin) |
| `static/styles.css` | Shared styles (mobile-first, light/dark) |
| `Dockerfile` | Python 3.11 slim + LibreOffice Calc + Poppler, runs as a non-root user |
| `render.yaml` | Render blueprint |

## Routes

| Method | Path | Access | Description |
| --- | --- | --- | --- |
| `GET`/`POST` | `/login` | public | Sign-in page / submit credentials |
| `POST` | `/logout` | signed in | Sign out |
| `GET` | `/` | user | Converter page |
| `POST` | `/api/convert` | user | Multipart `file`; returns `{"job_id": "..."}` |
| `GET` | `/api/jobs/{id}` | owner | `status`, `progress`, `message`, `error`, `jpeg_name` |
| `GET` | `/api/jobs/{id}/jpeg` | owner | The JPEG (`?download=true` for an attachment) |
| `POST` | `/api/events` | user | Browser analytics events (allow-listed) |
| `GET` | `/admin/analytics` | admin | Usage dashboard (`?days=1/7/30/90`) |
| `GET` | `/healthz` | public | Health check |

API errors are returned as `{"error": "..."}`.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `APP_USERS` | yes | JSON of users, e.g. `{"alice": {"password_hash": "pbkdf2_sha256$...", "role": "admin"}}` |
| `SESSION_SECRET` | yes in production | Key for signing session cookies (random per start if unset) |
| `SESSION_COOKIE_SECURE` | no | `true` makes cookies HTTPS-only (default `true` on Render, `false` elsewhere) |
| `MAX_UPLOAD_MB` | no | Upload size limit, default `20` |
| `ANALYTICS_DB_PATH` | no | SQLite path, default `data/analytics.db` |
| `JPEG_DPI`, `LIBREOFFICE_TIMEOUT`, `PDFTOPPM_TIMEOUT` | no | Defaults `300`, `120` s, `60` s |
| `PORT` | no | Listen port (Docker image default `10000`) |

Create a password hash for `APP_USERS`:

```bash
python auth.py hash-password
```

## Deployment (Render)

The app runs as a Docker web service on [Render](https://dashboard.render.com/).
`render.yaml` defines the service: create it with **New → Blueprint** and point it at this repo,
then set `APP_USERS` when prompted. `SESSION_SECRET` is
generated automatically, and `/healthz` is used as the health check.

On Render's free plan the filesystem is reset on every deploy and restart, so analytics history
is lost. To keep it, attach a persistent disk (paid plans) and point `ANALYTICS_DB_PATH` at it.

## Running locally

Requires Python 3.11+, LibreOffice and Poppler, or Docker:

```bash
docker build -t trs-converter .
docker run -p 10000:10000 -e APP_USERS='{"admin": {"password_hash": "...", "role": "admin"}}' trs-converter
```

## Limitations

- Only the first page of the workbook is rendered.
- Jobs are held in memory, so the app must run as a single instance. A restart drops jobs that are in progress.

## License

See [LICENSE](LICENSE).
