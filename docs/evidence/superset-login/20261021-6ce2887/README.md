# Intermittent browser login failure - Superset runtime (gevent workers)

Found by the quickstart viewer browser check, which failed on `ab396ba` (OL8) and `4f449c9` (Ubuntu) and passed
on `35c5bf4`, `4f449c9` (OL8) and `6ce2887` (Ubuntu). Investigated on the container kept from the `6ce2887`
Ubuntu run (BI redeployed there for debugging; not an acceptance run). `loop.py`: N independent browser contexts,
each logs in through the real login form and records every document/xhr response (with Set-Cookie and redirect).

| Workers | Account | Logins OK |
|---|---|---|
| 2 × gevent (pack default up to 0.1.0) | viewer | 19/20 (`loop-viewer-first20.json`), 38/40 (`loop-viewer.json`) |
| 2 × gevent | admin | 39/40 (`loop-admin.json`) |
| 1 × gevent | viewer | 79/80 (`loop-1worker.json`) |
| 2 × gthread, 8 threads | viewer | **100/100** (`loop-gthread.json`) |

Every failure has the same shape: `POST /login/` → 302 `/` with the session cookie set (the credentials were
accepted), `GET /superset/welcome/` → 302 `/login/?next=…`, `/api/v1/me/` → 401. No other response sets a cookie.
Superset logged `Failed to add user to db session: Class 'werkzeug.local.LocalProxy' is not mapped` 4 times under
gevent (5 failures) and 0 times under gthread (`superset-journal-excerpt.txt`). Admin fails too: not a viewer/RBAC
problem. Fix: `packs/superset` 0.1.1 runs gunicorn with `gthread` (the version bump re-applies the unit on
installed hosts).
