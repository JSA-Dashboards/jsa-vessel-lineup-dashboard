#!/usr/bin/env python3
"""
southport_poller.py -- runs on the JSA droplet via cron.

Uses the basis-tracker AZURE_CLIENT_ID (delegated, Mail.Read already granted)
with a persisted MSAL token cache for unattended silent refresh.

One-time setup: run setup_poller_auth.py interactively to create the token cache.

State: /opt/vessel-lineup-dashboard/poller_state.json
Log:   /opt/vessel-lineup-dashboard/poller.log

Cron: */30 12-23 * * 1-5 root /opt/vessel-lineup-dashboard/.venv/bin/python3 \
        /opt/vessel-lineup-dashboard/southport_poller.py \
        >> /opt/vessel-lineup-dashboard/poller.log 2>&1
"""

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msal
import requests

REPO_DIR   = Path(__file__).parent
ENV_FILE   = Path("/opt/basis-tracker/.env")
CACHE_FILE = REPO_DIR / ".token_cache.json"
STATE_FILE = REPO_DIR / "poller_state.json"
FACTS_DB   = REPO_DIR / "facts.db"
LOCK_FILE  = REPO_DIR / "logs" / ".facts.lock"      # shared with deploy/run_brazil.sh: one writer of facts.db + git at a time
LOG_FILE   = REPO_DIR / "poller.log"
CONVERT_PY = REPO_DIR / "convert_southport.py"
VENV_PY    = REPO_DIR / ".venv" / "bin" / "python3"

GRAPH  = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger(__name__)


def _parse_env(path):
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def load_cfg():
    env = _parse_env(ENV_FILE)
    env.update({k: v for k, v in os.environ.items()})
    required = ["AZURE_CLIENT_ID", "GRAPH_TENANT_ID", "GRAPH_INBOX_USER"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        log.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)
    return env


def get_token(cfg):
    if not CACHE_FILE.exists():
        log.error("Token cache not found: %s", CACHE_FILE)
        log.error("Run setup_poller_auth.py once interactively to create it.")
        sys.exit(1)

    cache = msal.SerializableTokenCache()
    cache.deserialize(CACHE_FILE.read_text())

    app = msal.PublicClientApplication(
        client_id=cfg["AZURE_CLIENT_ID"],
        authority="https://login.microsoftonline.com/" + cfg["GRAPH_TENANT_ID"],
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if not accounts:
        log.error("No accounts in token cache. Run setup_poller_auth.py again.")
        sys.exit(1)

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        log.error("Silent token refresh failed: %s", result)
        log.error("Re-run setup_poller_auth.py to refresh the login.")
        sys.exit(1)

    if cache.has_state_changed:
        CACHE_FILE.write_text(cache.serialize())

    return result["access_token"]


def graph_get(token, path, **params):
    r = requests.get(
        GRAPH + path,
        headers={"Authorization": "Bearer " + token},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def graph_get_bytes(token, path):
    r = requests.get(
        GRAPH + path,
        headers={"Authorization": "Bearer " + token},
        timeout=60,
    )
    r.raise_for_status()
    return r.content


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_processed_id": None, "last_processed_time": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _page(token, url, params=None):
    r = requests.get(url, headers={"Authorization": "Bearer " + token}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_new_southport_emails(token, inbox_user, processed_ids, days=4, max_pages=20):
    """Every Southport line-up email from the last `days` days that has not been processed, OLDEST FIRST.
    Scans by date, not by 'newest N': the Inbox takes ~200 messages a day, so a Southport file that arrived
    in the morning is buried far below the newest 25 by the time the poller runs (the Oct 13 file sat at
    position 193). [(message id, subject, received, attachment id, attachment name)]"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = GRAPH + "/users/" + inbox_user + "/mailFolders/inbox/messages"
    params = {"$filter": "receivedDateTime ge " + since, "$orderby": "receivedDateTime desc", "$top": "100",
              "$select": "id,subject,receivedDateTime,from,hasAttachments"}
    found = []
    for _ in range(max_pages):
        data = _page(token, url, params)
        params = None
        for msg in data.get("value", []):
            mid = msg["id"]
            if mid in processed_ids or not msg.get("hasAttachments"):
                continue
            subj = msg.get("subject") or ""
            addr = (msg.get("from") or {}).get("emailAddress", {})
            if "southport" not in ((addr.get("name") or "") + " " + (addr.get("address") or "")).lower() \
                    and "southport" not in subj.lower():
                continue
            atts = graph_get(token, "/users/" + inbox_user + "/messages/" + mid + "/attachments",
                             **{"$select": "id,name"}).get("value", [])
            # only the daily line-up workbook: Southport also forwards monthly STATS & CHARTS workbooks, which are
            # not line-ups and cannot be converted (one of them once blocked the real line-up behind it)
            xlsx = [a for a in atts if a.get("name", "").lower().endswith(".xlsx") and "lineup" in a.get("name", "").lower().replace(" ", "").replace("-", "")]
            if not xlsx:
                continue
            log.info("Candidate: [%s] (%s) -> %s", subj, msg.get("receivedDateTime", ""), xlsx[0]["name"])
            found.append((mid, subj, msg.get("receivedDateTime", ""), xlsx[0]["id"], xlsx[0]["name"]))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return sorted(found, key=lambda x: x[2])


def download_attachment(token, inbox_user, msg_id, att_id):
    content = graph_get_bytes(
        token,
        "/users/" + inbox_user + "/messages/" + msg_id + "/attachments/" + att_id + "/$value"
    )
    tmp = Path(tempfile.mktemp(suffix=".xlsx"))
    tmp.write_bytes(content)
    return tmp


def run_converter(tmp_path):
    result = subprocess.run(
        [str(VENV_PY), str(CONVERT_PY), str(tmp_path), "--no-push"],
        cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    if result.stdout:
        log.info(result.stdout.strip())
    if result.returncode != 0:
        log.error("Converter failed:\n%s", result.stderr)
        raise RuntimeError("convert_southport.py failed")


@contextlib.contextmanager
def facts_lock():
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def update_facts(xlsx_path, att_name, recv_time):
    """Bank this file as a line-up snapshot + sail events in facts.db. A failure here is logged and
    never blocks the workbook push below."""
    try:
        sys.path.insert(0, str(REPO_DIR))
        from fact_model import ingest
        res = ingest.ingest_southport_file(str(xlsx_path), name=att_name, received=recv_time, facts_db=str(FACTS_DB))
        log.info("facts.db updated: %s", res)
    except Exception:
        log.exception("facts.db update failed (workbook push continues)")


def git_push(subject):
    to_add = ["Vessel Lineup - US.xlsx"] + (["facts.db"] if FACTS_DB.exists() else [])
    for cmd in [
        ["git", "-C", str(REPO_DIR), "add", *to_add],
        ["git", "-C", str(REPO_DIR), "commit", "-m",
         "Update vessel lineup from Southport: " + subject,
         "--author", "Kolten Postin <275148418+koltenpostin93-blip@users.noreply.github.com>"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            if "nothing to commit" in r.stdout + r.stderr:
                log.info("Nothing to commit.")
                return
            log.error("Git error: %s", r.stderr or r.stdout)
            raise RuntimeError("git commit failed")

    # Push — if remote is ahead, pull --rebase first then retry once
    for attempt in range(2):
        r = subprocess.run(
            ["git", "-C", str(REPO_DIR), "push"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            log.info("Pushed to GitHub. Streamlit Cloud will refresh in ~1 minute.")
            return
        if attempt == 0 and ("fetch first" in r.stderr or "rejected" in r.stderr):
            rb = subprocess.run(
                ["git", "-C", str(REPO_DIR), "pull", "--rebase"],
                capture_output=True, text=True,
            )
            if rb.returncode != 0:
                log.error("Pull --rebase failed: %s", rb.stderr)
                raise RuntimeError("git pull --rebase failed")
            continue
        log.error("Git push failed: %s", r.stderr or r.stdout)
        raise RuntimeError("git push failed")


def main():
    cfg   = load_cfg()
    state = load_state()
    log.info("Polling inbox: %s", cfg["GRAPH_INBOX_USER"])

    processed = set(state.get("processed_ids", []))
    if state.get("last_processed_id"):
        processed.add(state["last_processed_id"])

    token = get_token(cfg)
    emails = find_new_southport_emails(token, cfg["GRAPH_INBOX_USER"], processed)

    if not emails:
        log.info("No new Southport emails found.")
        return

    log.info("%d new Southport email(s): %s", len(emails), "; ".join(e[4] for e in emails))
    done, last = [], None
    with facts_lock():
        for msg_id, subject, recv_time, att_id, att_name in emails:         # oldest first: the workbook ends up as the newest
            tmp = download_attachment(token, cfg["GRAPH_INBOX_USER"], msg_id, att_id)
            try:
                run_converter(tmp)
                update_facts(tmp, att_name, recv_time)
                done.append(msg_id)
                last = (msg_id, subject, recv_time)
            except Exception:
                log.exception("Could not process %s; it will be retried next run", att_name)
                continue                                                    # one bad file must not block the newer ones
            finally:
                tmp.unlink(missing_ok=True)
        if last:
            git_push(last[1])
    if last:
        processed.update(done)
        state["processed_ids"] = sorted(processed)[-500:]
        state["last_processed_id"], state["last_processed_time"] = last[0], last[2]
        save_state(state)
        log.info("Done. %d file(s) processed; state updated.", len(done))


if __name__ == "__main__":
    main()
