"""
brazil_lineup_import.py  –  Droplet cron script.
Fetches APS Brazil line-up PDF attachments from email via Microsoft Graph
and loads them into brazil_lineup.db.

Usage:
    python brazil_lineup_import.py            # check last 50 inbox emails
    python brazil_lineup_import.py --backfill # process ALL matching emails
"""

import argparse
import fnmatch
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Env / paths
# ---------------------------------------------------------------------------
ENV_FILE = "/opt/basis-tracker/.env"
TOKEN_CACHE = "/opt/vessel-lineup-dashboard/.token_cache.json"
STATE_FILE = "/opt/vessel-lineup-dashboard/brazil_poller_state.json"
DB_FILE = "/opt/vessel-lineup-dashboard/brazil_lineup.db"
LOG_FILE = "/opt/vessel-lineup-dashboard/brazil_poller.log"

# ---------------------------------------------------------------------------
# Logging (FileHandler only – no StreamHandler)
# ---------------------------------------------------------------------------
log = logging.getLogger("brazil_poller")
log.setLevel(logging.INFO)

def _setup_logging():
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------

def _load_env(env_file=ENV_FILE):
    if not os.path.exists(env_file):
        log.warning(f"Env file not found: {env_file}")
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


# ---------------------------------------------------------------------------
# Graph auth
# ---------------------------------------------------------------------------

SCOPES = ["https://graph.microsoft.com/.default"]


def _get_access_token():
    """Acquire token via delegated auth (PublicClientApplication + cached token).
    Token cache is shared with southport_poller.py; run setup_poller_auth.py once
    interactively to create it.
    """
    import msal  # type: ignore

    if not os.path.exists(TOKEN_CACHE):
        raise RuntimeError(
            f"Token cache not found: {TOKEN_CACHE}. "
            "Run setup_poller_auth.py interactively once to create it."
        )

    client_id = os.environ["AZURE_CLIENT_ID"]
    tenant_id = os.environ["GRAPH_TENANT_ID"]
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    cache = msal.SerializableTokenCache()
    with open(TOKEN_CACHE) as f:
        cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        client_id,
        authority=authority,
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError(
            "No accounts in token cache. Run setup_poller_auth.py again."
        )

    result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if cache.has_state_changed:
        try:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
        except Exception as e:
            log.warning(f"Could not write token cache: {e}")

    if not result or "access_token" not in result:
        raise RuntimeError(
            f"Silent token refresh failed: {result}. "
            "Re-run setup_poller_auth.py to refresh the login."
        )
    return result["access_token"]


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _graph_get(token, url, params=None):
    import requests  # type: ignore

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _graph_get_bytes(token, url):
    import requests  # type: ignore

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Email matching
# ---------------------------------------------------------------------------
_APS_DOMAIN = "@agriportservices.com.br"
_PDF_PATTERNS = ("APS_Brz_*.pdf", "APS Brz*.pdf")


def _attachment_name_matches(name):
    name_lower = name.lower()
    for pat in _PDF_PATTERNS:
        if fnmatch.fnmatch(name_lower, pat.lower()):
            return True
    return False


def _email_is_candidate(msg):
    """True if message is from APS (or forwarded from) AND has attachments."""
    if not msg.get("hasAttachments"):
        return False
    sender = (
        (msg.get("from") or {})
        .get("emailAddress", {})
        .get("address", "")
        .lower()
    )
    body_preview = (msg.get("bodyPreview") or "").lower()
    return _APS_DOMAIN in sender or _APS_DOMAIN in body_preview


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_id": None}


def _save_state(state):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(backfill=False):
    import brazil_parser  # type: ignore
    import brazil_db  # type: ignore

    brazil_db.init_db(DB_FILE)

    token = _get_access_token()
    inbox_user = os.environ["GRAPH_INBOX_USER"]
    base = f"https://graph.microsoft.com/v1.0/users/{inbox_user}"

    # Fetch top 50 emails (no $filter – Graph 400s on filter+orderby)
    msgs_url = f"{base}/mailFolders/inbox/messages"
    params = {
        "$top": 50,
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,hasAttachments,from,bodyPreview,receivedDateTime",
    }
    data = _graph_get(token, msgs_url, params)
    messages = data.get("value", [])
    log.info(f"Fetched {len(messages)} inbox messages")

    state = _load_state()
    last_id = state["last_processed_id"]

    processed_ids = []

    for msg in messages:
        msg_id = msg["id"]

        # Non-backfill: stop at previously seen ID
        if not backfill and last_id and msg_id == last_id:
            log.info(f"Reached last processed message {msg_id[:16]}…, stopping")
            break

        if not _email_is_candidate(msg):
            continue

        # Fetch attachments
        att_url = f"{base}/messages/{msg_id}/attachments"
        try:
            att_data = _graph_get(token, att_url)
        except Exception as e:
            log.warning(f"Could not fetch attachments for {msg_id[:16]}: {e}")
            continue

        for att in att_data.get("value", []):
            att_name = att.get("name", "")
            if not _attachment_name_matches(att_name):
                continue

            log.info(f"Processing attachment: {att_name} (msg {msg_id[:16]}…)")

            # Download attachment bytes
            att_id = att["id"]
            att_url_dl = f"{base}/messages/{msg_id}/attachments/{att_id}/$value"
            try:
                pdf_bytes = _graph_get_bytes(token, att_url_dl)
            except Exception as e:
                log.error(f"Download failed for {att_name}: {e}")
                continue

            # Write to temp file and parse
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(pdf_bytes)
                    tmp_path = tmp.name

                parsed = brazil_parser.parse_pdf(tmp_path)
            except Exception as e:
                log.error(f"Parse failed for {att_name}: {e}")
                continue
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            rd = parsed["report_date"]
            if rd is None:
                log.warning(f"Could not extract report date from {att_name}, skipping")
                continue

            rd_str = rd.isoformat() if hasattr(rd, "isoformat") else str(rd)

            if brazil_db.date_loaded(rd_str, DB_FILE):
                log.info(f"Date {rd_str} already loaded, skipping")
                continue

            brazil_db.upsert_report(parsed, DB_FILE)
            n_lineup = len(parsed["lineup"])
            n_sailed = len(parsed["sailed"])
            log.info(
                f"Loaded {n_lineup} lineup rows, {n_sailed} sailed rows for date {rd_str}"
            )
            processed_ids.append(msg_id)

    # Update state (non-backfill only: track newest processed ID)
    if not backfill and processed_ids:
        state["last_processed_id"] = processed_ids[0]  # messages are newest-first
        _save_state(state)
        log.info(f"State updated: last_processed_id={processed_ids[0][:16]}…")
    elif not backfill and messages:
        # No new messages processed; update last_id to current newest if state is empty
        if not last_id:
            state["last_processed_id"] = messages[0]["id"]
            _save_state(state)


if __name__ == "__main__":
    _setup_logging()
    parser = argparse.ArgumentParser(description="Brazil lineup email importer")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process all matching emails (don't update state)",
    )
    args = parser.parse_args()

    _load_env()

    try:
        run(backfill=args.backfill)
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
