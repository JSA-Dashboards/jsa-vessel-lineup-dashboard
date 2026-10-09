"""Runtime data fetch: ETag handling, atomic/validated writes, fallbacks, and the cache-key guard.

    pytest tests/test_data_sync.py -v
"""

import ast
import hashlib
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import data_sync as ds


class Repo:
    """A tiny raw.githubusercontent stand-in: serves files with ETags and honours If-None-Match."""

    def __init__(self, root):
        self.root, self.hits, self.mode = root, [], "ok"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = os.path.basename(self.path.split("?")[0]).replace("%20", " ")
                outer.hits.append((name, self.headers.get("If-None-Match")))
                if outer.mode == "down":
                    self.send_response(503); self.end_headers(); return
                p = os.path.join(outer.root, name)
                if not os.path.exists(p):
                    self.send_response(404); self.end_headers(); return
                body = b"<html>rate limited</html>" if outer.mode == "html" else open(p, "rb").read()
                etag = '"' + hashlib.md5(body).hexdigest() + '"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304); self.end_headers(); return
                self.send_response(200); self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def make_db(path, n):
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path); c.execute("create table t(x)"); c.executemany("insert into t values (?)", [(i,) for i in range(n)]); c.commit(); c.close()


@pytest.fixture
def env(tmp_path):
    remote = tmp_path / "remote"; remote.mkdir()
    app = tmp_path / "app"; app.mkdir()
    make_db(str(remote / "facts.db"), 3)
    make_db(str(app / "facts.db"), 1)                                   # the copy bundled with the deploy
    repo = Repo(str(remote))
    yield {"repo": repo, "remote": remote, "app": str(app), "cache": str(tmp_path / "cache")}
    repo.close()


def rows(path):
    return sqlite3.connect(path).execute("select count(*) from t").fetchone()[0]


def fetch(env, name="facts.db"):
    return ds.fetch_one(name, env["app"], base_url=env["repo"].url, cache_dir=env["cache"], timeout=5)


def test_downloads_then_uses_etag_so_an_unchanged_file_is_not_downloaded_again(env):
    a = fetch(env)
    assert a["source"] == "remote" and rows(a["path"]) == 3
    b = fetch(env)
    assert b["source"] == "remote-cached" and b["path"] == a["path"]
    assert env["repo"].hits[-1][1] is not None                          # second request was conditional


def test_describe_reports_the_state_of_the_data(env):
    info = {"facts.db": fetch(env)}
    assert ds.describe(info).startswith("Data: live from GitHub, checked ")
    env["repo"].mode = "down"
    assert "unreachable" in ds.describe({"facts.db": fetch(env)})
    assert "bundled copy" in ds.describe({"x": fetch(env, "facts.db") | {"source": "bundled"}})


def test_sync_checks_all_files_and_returns_one_entry_each(env):
    make_db(str(env["remote"] / "brazil_lineup.db"), 2)
    import shutil
    shutil.copy(env["app"] + "/facts.db", env["app"] + "/brazil_lineup.db")
    os.environ["VESSEL_DATA_SOURCE"] = "remote"
    try:
        info = ds.sync(env["app"], names=("facts.db", "brazil_lineup.db"), base_url=env["repo"].url, cache_dir=env["cache"])
    finally:
        del os.environ["VESSEL_DATA_SOURCE"]
    assert set(info) == {"facts.db", "brazil_lineup.db"} and all(i["source"] == "remote" for i in info.values())


def test_a_changed_file_is_picked_up(env):
    fetch(env)
    make_db(str(env["remote"] / "facts.db"), 9)
    c = fetch(env)
    assert c["source"] == "remote" and rows(c["path"]) == 9


def test_github_down_falls_back_to_the_last_download_then_to_the_bundled_copy(env):
    env["repo"].mode = "down"
    first = fetch(env)                                                   # nothing downloaded yet -> bundled
    assert first["source"] == "bundled" and first["path"] == os.path.join(env["app"], "facts.db") and "503" in first["error"]
    env["repo"].mode = "ok"; fetch(env)                                  # now there is a download
    env["repo"].mode = "down"
    again = fetch(env)
    assert again["source"] == "stale-cache" and rows(again["path"]) == 3


def test_an_error_page_is_never_cached_as_data(env):
    env["repo"].mode = "html"
    r = fetch(env)
    assert r["source"] == "bundled" and "not a valid file" in r["error"]
    assert not any(f.endswith(".part") for f in os.listdir(env["cache"]))   # no leftovers
    assert not os.path.exists(os.path.join(env["cache"], "facts.db"))


def test_a_corrupt_download_does_not_replace_a_good_copy(env):
    good = fetch(env)
    env["repo"].mode = "html"
    make_db(str(env["remote"] / "facts.db"), 4)                          # content changes, but the server now returns junk
    r = fetch(env)
    assert r["source"] == "stale-cache" and rows(good["path"]) == 3


def test_missing_file_on_the_remote_uses_the_bundled_copy(env):
    r = ds.fetch_one("brazil_lineup.db", env["app"], base_url=env["repo"].url, cache_dir=env["cache"], timeout=5)
    assert r["source"] == "bundled" and "404" in r["error"]


def test_local_mode_touches_nothing(env, monkeypatch):
    monkeypatch.setenv("VESSEL_DATA_SOURCE", "local")
    info = ds.sync(env["app"], names=("facts.db",), base_url=env["repo"].url, cache_dir=env["cache"])
    assert info["facts.db"]["source"] == "local" and env["repo"].hits == [] and not os.path.exists(env["cache"])
    assert ds.describe(info) == "Data: local files"


def test_remote_is_auto_enabled_only_on_streamlit_cloud(monkeypatch):
    monkeypatch.delenv("VESSEL_DATA_SOURCE", raising=False)
    assert ds.remote_enabled("/mount/src/jsa-vessel-lineup-dashboard") is True
    assert ds.remote_enabled(r"C:\Users\someone\Desktop\vessel-lineup-dashboard") is False
    monkeypatch.setenv("VESSEL_DATA_SOURCE", "remote")
    assert ds.remote_enabled(r"C:\anything") is True


def test_workbook_names_with_spaces_are_requested_correctly(env):
    import zipfile
    with zipfile.ZipFile(env["remote"] / "Vessel Lineup - US.xlsx", "w") as z:
        z.writestr("x.txt", "hi")
    r = ds.fetch_one("Vessel Lineup - US.xlsx", env["app"], base_url=env["repo"].url, cache_dir=env["cache"], timeout=5)
    assert r["source"] == "remote" and os.path.getsize(r["path"]) > 0


# ── the cache-key trap ───────────────────────────────────────────────────────

@pytest.mark.parametrize("fname", ["app.py", "region_views.py"])
def test_cached_loaders_key_on_the_file_time(fname):
    """st.cache_data ignores arguments that start with an underscore, so a loader whose file-modified-time
    argument is underscored would keep serving stale data after the file changes."""
    tree = ast.parse(open(os.path.join(HERE, fname), encoding="utf-8").read())
    seen = 0
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if any("cache_data" in ast.unparse(d) for d in fn.decorator_list):
            for a in fn.args.args + fn.args.kwonlyargs:
                if "mtime" in a.arg:
                    seen += 1
                    assert not a.arg.startswith("_"), f"{fname}:{fn.name}({a.arg}) is not part of the cache key"
    assert seen >= 3
