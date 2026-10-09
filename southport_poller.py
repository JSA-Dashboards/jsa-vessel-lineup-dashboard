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


def find_new_southport_email(token, inbox_user, last_id):
    # Note: $filter + $orderby together returns 400; filter hasAttachments in Python
    data = graph_get(
        token,
        "/users/" + inbox_user + "/mailFolders/inbox/messages",
        **{
            "$top": "25",
            "$select": "id,subject,receivedDateTime,from,hasAttachments",
            "$orderby": "receivedDateTime desc",
        }
    )
    for msg in data.get("value", []):
        mid         = msg["id"]
        subj        = msg.get("subject", "")
        recv        = msg.get("receivedDateTime", "")
        sender_name = msg.get("from", {}).get("emailAddress", {}).get("name", "")
        sender_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")

        if mid == last_id:
            break

        if not msg.get("hasAttachments"):
            continue

        combined = (sender_name + " " + sender_addr).lower()
        if "southport" not in combined and "southport" not in subj.lower():
            continue

        log.info("Candidate: [%s] from %s (%s)", subj, sender_name, recv)

        att_data = graph_get(token,
            "/users/" + inbox_user + "/messages/" + mid + "/attachments",
            **{"$select": "id,name"})
        for att in att_data.get("value", []):
            if att.get("name", "").lower().endswith(".xlsx"):
                return mid, subj, recv, att["id"], att["name"]

    return None, None, None, None, None


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

    token = get_token(cfg)
    msg_id, subject, recv_time, att_id, att_name = find_new_southport_email(
        token, cfg["GRAPH_INBOX_USER"], state.get("last_processed_id")
    )

    if msg_id is None:
        log.info("No new Southport emails found.")
        return

    log.info("New email: [%s]  attachment: %s", subject, att_name)
    tmp = download_attachment(token, cfg["GRAPH_INBOX_USER"], msg_id, att_id)
    try:
        with facts_lock():
            run_converter(tmp)
            update_facts(tmp, att_name, recv_time)
            git_push(subject)
        state["last_processed_id"]   = msg_id
        state["last_processed_time"] = recv_time
        save_state(state)
        log.info("Done. State updated.")
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
