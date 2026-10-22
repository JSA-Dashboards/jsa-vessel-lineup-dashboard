#!/usr/bin/env python3
"""
argentina_poller.py -- runs on the JSA droplet via cron (see deploy/argentina-poller.cron).

Picks up the Nabsa agency e-mail 'DAILY VSL LINE UP / SAILED VSLS' (tlxoper@nabsa.com.ar, sent by
mjlandi@nabsa.com.ar), which carries lineupYYYYMMDD.xlsx and sailedYYYYMMDD.xlsx, and banks them in facts.db
(Argentina line-up snapshot + sail events), then commits and pushes facts.db.

Shares the Graph login, lock and repo with southport_poller.py. Own state: argentina_state.json.
A date window is scanned (not 'newest N'): the Inbox takes ~200 messages a day.
"""

import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import southport_poller as sp

log = logging.getLogger("argentina_poller")
STATE = sp.REPO_DIR / "argentina_state.json"
SENDER_HINT = "nabsa"


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"processed_ids": []}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def find_new_emails(token, inbox_user, processed_ids, days=4, max_pages=20):
    """Unprocessed Nabsa messages of the last `days` days that carry a line-up workbook, OLDEST FIRST:
    [(message id, subject, received, {'lineup': (att id, name), 'sailed': (att id, name)})]."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = sp.GRAPH + "/users/" + inbox_user + "/mailFolders/inbox/messages"
    params = {"$filter": "receivedDateTime ge " + since, "$orderby": "receivedDateTime desc", "$top": "100",
              "$select": "id,subject,receivedDateTime,from,hasAttachments"}
    found = []
    for _ in range(max_pages):
        data = sp._page(token, url, params)
        params = None
        for msg in data.get("value", []):
            mid = msg["id"]
            if mid in processed_ids or not msg.get("hasAttachments"):
                continue
            addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
            if SENDER_HINT not in addr:
                continue
            atts = sp.graph_get(token, "/users/" + inbox_user + "/messages/" + mid + "/attachments",
                                **{"$select": "id,name"}).get("value", [])
            pick = {}
            for a in atts:
                n = a.get("name", "").replace("\x00", "").lower()
                if n.endswith(".xlsx") and n.startswith("lineup"):
                    pick["lineup"] = (a["id"], a["name"])
                elif n.endswith(".xlsx") and n.startswith("sailed"):
                    pick["sailed"] = (a["id"], a["name"])
            if pick:
                log.info("Candidate: (%s) %s", msg.get("receivedDateTime", ""), sorted(pick))
                found.append((mid, msg.get("subject") or "", msg.get("receivedDateTime", ""), pick))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return sorted(found, key=lambda x: x[2])


def git_push(label):
    repo = str(sp.REPO_DIR)
    for cmd in (["git", "-C", repo, "add", "facts.db"],
                ["git", "-C", repo, "commit", "-m", "Argentina lineup + facts: " + label,
                 "--author", "Kolten Postin <275148418+koltenpostin93-blip@users.noreply.github.com>"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            if "nothing to commit" in r.stdout + r.stderr:
                log.info("Nothing to commit.")
                return
            raise RuntimeError("git failed: " + (r.stderr or r.stdout))
    for attempt in range(2):
        r = subprocess.run(["git", "-C", repo, "push"], capture_output=True, text=True)
        if r.returncode == 0:
            log.info("Pushed to GitHub.")
            return
        if attempt == 0 and ("fetch first" in r.stderr or "rejected" in r.stderr):
            rb = subprocess.run(["git", "-C", repo, "pull", "--rebase", "--autostash"], capture_output=True, text=True)
            if rb.returncode != 0:
                raise RuntimeError("pull --rebase failed: " + rb.stderr)
            continue
        raise RuntimeError("git push failed: " + (r.stderr or r.stdout))


def _download(token, user, msg_id, att_id):
    content = sp.graph_get_bytes(token, "/users/" + user + "/messages/" + msg_id + "/attachments/" + att_id + "/$value")
    tmp = Path(tempfile.mktemp(suffix=".xlsx"))
    tmp.write_bytes(content)
    return tmp


def main():
    cfg = sp.load_cfg()
    state = load_state()
    processed = set(state.get("processed_ids", []))
    token = sp.get_token(cfg)
    user = cfg["GRAPH_INBOX_USER"]
    emails = find_new_emails(token, user, processed)
    if not emails:
        log.info("No new Argentina (Nabsa) emails found.")
        return

    sys.path.insert(0, str(sp.REPO_DIR))
    from fact_model import argentina
    done, last = [], None
    with sp.facts_lock():
        for msg_id, subject, recv, pick in emails:
            tmps = {k: _download(token, user, msg_id, v[0]) for k, v in pick.items()}
            try:
                date = argentina.report_date_from_name(pick.get("lineup", pick.get("sailed"))[1])
                res = argentina.ingest_argentina_files(tmps.get("lineup"), tmps.get("sailed"), report_date=date,
                                                       facts_db=str(sp.FACTS_DB))
                log.info("facts.db updated: %s", res)
                done.append(msg_id)
                last = (date, recv)
            except Exception:
                log.exception("Could not process Nabsa email received %s; it will be retried next run", recv)
                break
            finally:
                for t in tmps.values():
                    t.unlink(missing_ok=True)
        if last:
            git_push(f"{last[0]:%Y-%m-%d}")
    if last:
        processed.update(done)
        state["processed_ids"] = sorted(processed)[-500:]
        save_state(state)
        log.info("Done. %d email(s) processed.", len(done))


if __name__ == "__main__":
    main()
