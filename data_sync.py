"""Keep the app's data current without a redeploy.

On Streamlit Community Cloud the app only sees the files that were in the repo when it last deployed, and
this repo's pushes (the droplet commits the databases daily) do not trigger a redeploy. So on Cloud the
app downloads the data files from the public repo at runtime instead.

- Source: raw.githubusercontent.com/<repo>/<branch>/<file>. Conditional (ETag) requests: an unchanged file
  costs a 304, not a 26 MB download.
- Writes are atomic (download to a .part file, check it is really a SQLite / xlsx file, then rename) so a
  session reading the old copy never sees a half-written one and a GitHub error page is never cached.
- If GitHub is unreachable the last downloaded copy is used, else the copy bundled with the deploy.
- Locally (not on Cloud) nothing is fetched: the files next to app.py are used as they are. Force either
  way with VESSEL_DATA_SOURCE=remote|local.
"""

import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

REPO = os.environ.get("VESSEL_DATA_REPO", "JSA-Dashboards/jsa-vessel-lineup-dashboard")
BRANCH = os.environ.get("VESSEL_DATA_BRANCH", "master")
BASE_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
FILES = ("brazil_lineup.db", "facts.db", "Vessel Lineup - US.xlsx")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "vessel_lineup_data")
MAGIC = {".db": b"SQLite format 3\x00", ".xlsx": b"PK\x03\x04"}


def remote_enabled(app_dir):
    """Remote fetch on Streamlit Cloud (app lives under /mount/src), or when forced."""
    mode = os.environ.get("VESSEL_DATA_SOURCE", "auto").lower()
    if mode in ("remote", "local"):
        return mode == "remote"
    return str(app_dir).replace("\\", "/").startswith("/mount/src")


def _looks_valid(path, name):
    magic = MAGIC.get(os.path.splitext(name)[1].lower())
    if magic is None:
        return os.path.getsize(path) > 0
    with open(path, "rb") as f:
        return f.read(len(magic)) == magic


def _read_meta(dest):
    try:
        with open(dest + ".json") as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_one(name, app_dir, base_url=None, cache_dir=None, timeout=60):
    """Make sure the newest copy of `name` is on disk. Returns
    {name, path, source, fetched_at, error}; source is 'remote' (downloaded just now), 'remote-cached'
    (GitHub says unchanged), 'stale-cache' (GitHub unreachable, earlier download used) or 'bundled'."""
    import requests
    base_url = base_url or BASE_URL
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, name)
    meta = _read_meta(dest) if os.path.exists(dest) else {}
    headers = {"User-Agent": "jsa-vessel-lineup-dashboard"}
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    tmp = None
    try:
        r = requests.get(base_url + quote(name), headers=headers, timeout=timeout, stream=True)
        if r.status_code == 304 and os.path.exists(dest):
            meta["checked_at"] = time.time()
            with open(dest + ".json", "w") as f:
                json.dump(meta, f)
            return {"name": name, "path": dest, "source": "remote-cached", "fetched_at": meta.get("fetched_at"),
                    "checked_at": meta["checked_at"], "error": None}
        r.raise_for_status()
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".part")
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        if not _looks_valid(tmp, name):
            raise ValueError(f"{name}: downloaded content is not a valid file")
        os.replace(tmp, dest)
        tmp = None
        now = time.time()
        with open(dest + ".json", "w") as f:
            json.dump({"etag": r.headers.get("ETag"), "fetched_at": now, "checked_at": now}, f)
        return {"name": name, "path": dest, "source": "remote", "fetched_at": now, "checked_at": now, "error": None}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:200]
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(dest):
            return {"name": name, "path": dest, "source": "stale-cache", "fetched_at": meta.get("fetched_at"),
                    "checked_at": meta.get("checked_at"), "error": err}
        return {"name": name, "path": os.path.join(app_dir, name), "source": "bundled", "fetched_at": None,
                "checked_at": None, "error": err}


def sync(app_dir, names=FILES, base_url=None, cache_dir=None):
    """{file name: info} for every data file. Local mode returns the files beside app.py untouched."""
    if not remote_enabled(app_dir):
        return {n: {"name": n, "path": os.path.join(app_dir, n), "source": "local", "fetched_at": None,
                    "checked_at": None, "error": None} for n in names}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:            # three small round trips in parallel, not in series
        results = list(pool.map(lambda n: fetch_one(n, app_dir, base_url, cache_dir), names))
    return dict(zip(names, results))


def describe(info):
    """One short line for the sidebar."""
    sources = {i["source"] for i in info.values()}
    if sources == {"local"}:
        return "Data: local files"
    errs = [i["name"] for i in info.values() if i["error"]]
    newest = max((i["checked_at"] for i in info.values() if i.get("checked_at")), default=None)
    stamp = time.strftime("%b %d %H:%M UTC", time.gmtime(newest)) if newest else "n/a"
    if "bundled" in sources:
        return f"Data: bundled copy (GitHub unreachable: {', '.join(errs)})"
    if "stale-cache" in sources:
        return f"Data: last checked {stamp} (GitHub unreachable now)"
    return f"Data: live from GitHub, checked {stamp}"
