# Project Memory

- FastAPI teaching demo (Chinese UI, `lang="zh-CN"`); sessions manually stored in Redis Hash key `session:<id>` with TTL — do not migrate to FastAPI's built-in cookie sessions.
- Requires a local Redis on `localhost:6379`; `/inspect` page surfaces connection status via `redis_ok` flag — verify Redis is up before demoing.
- Two-file core: `app.py` (routes, login/logout, `Depends`-based auth guard) and `session_store.py` (Redis Hash CRUD abstraction); no tests exist and `pytest` is not in `requirements.txt`.
- Deps pinned loosely: `fastapi>=0.110`, `uvicorn[standard]>=0.27`, `jinja2>=3.1`, `redis>=5.0` — run with `uvicorn app:app --reload` (not Flask, even though templates use `url_for`).
- Templates (`base.html`, `home.html`, `login.html`, `redis_inspect.html`) extend a single base and use Starlette's `url_for`; keep new pages consistent with this block structure.
- Pitfall: `session_store.py` reads with `HMGET` — any new session field must be added both on write (login) and read (auth dependency) or it silently returns `None`.
