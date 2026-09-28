"""
fetch_southport_email.py
Finds the most recent Southport vessel-lineup email in Outlook,
saves the Excel attachment as 'Vessel Lineup - US.xlsx' in this folder,
then commits and pushes to GitHub.
"""

import os
import sys
import subprocess
import tempfile
import shutil

# ── Configuration ─────────────────────────────────────────────────────────────
# Adjust SENDER_FILTER or SUBJECT_FILTER if the match isn't picking up the email.
# Matching is case-insensitive; leave a filter blank ("") to skip it.
SENDER_FILTER  = "southport"   # matches any part of the sender name/address
SUBJECT_FILTER = ""            # e.g. "vessel lineup" — leave blank to match any subject

REPO_DIR  = os.path.dirname(os.path.abspath(__file__))
DEST_FILE = os.path.join(REPO_DIR, "Vessel Lineup - US.xlsx")
# ──────────────────────────────────────────────────────────────────────────────


def find_southport_email():
    """Return the most recent Outlook mail item that matches the filters and
    has an .xlsx attachment.  Returns None if nothing matches."""
    try:
        import win32com.client
    except ImportError:
        print("ERROR: pywin32 is not installed.")
        print("Run:  pip install pywin32")
        sys.exit(1)

    outlook = win32com.client.Dispatch("Outlook.Application")
    ns      = outlook.GetNamespace("MAPI")

    # Search Inbox; adjust folder index if your inbox is elsewhere
    inbox = ns.GetDefaultFolder(6)  # 6 = olFolderInbox

    # Build a DASL restriction to pre-filter on the server side
    parts = []
    if SENDER_FILTER:
        f = SENDER_FILTER.lower()
        parts.append(
            f"(\"urn:schemas:httpmail:fromname\" LIKE '%{f}%' OR "
            f"\"urn:schemas:httpmail:fromemail\" LIKE '%{f}%')"
        )
    if SUBJECT_FILTER:
        parts.append(
            f"\"urn:schemas:httpmail:subject\" LIKE '%{SUBJECT_FILTER.lower()}%'"
        )

    if parts:
        restriction = " AND ".join(parts)
        items = inbox.Items.Restrict(restriction)
    else:
        items = inbox.Items

    items.Sort("[ReceivedTime]", True)  # newest first

    for item in items:
        try:
            if item.Class != 43:   # 43 = olMail
                continue
            for att in item.Attachments:
                if att.FileName.lower().endswith(".xlsx"):
                    return item, att
        except Exception:
            continue

    return None, None


def main():
    print("Searching Outlook for the latest Southport vessel lineup email...")
    mail_item, attachment = find_southport_email()

    if mail_item is None:
        print("\nNo matching email found.")
        print(f"  Sender filter : '{SENDER_FILTER}' (blank = any)")
        print(f"  Subject filter: '{SUBJECT_FILTER}' (blank = any)")
        print("Edit SENDER_FILTER / SUBJECT_FILTER at the top of this script if needed.")
        input("\nPress Enter to exit.")
        sys.exit(1)

    sender  = getattr(mail_item, "SenderName",    "unknown sender")
    subject = getattr(mail_item, "Subject",        "(no subject)")
    recv    = getattr(mail_item, "ReceivedTime",   "unknown time")
    fname   = attachment.FileName

    print(f"\nFound email:")
    print(f"  From    : {sender}")
    print(f"  Subject : {subject}")
    print(f"  Received: {recv}")
    print(f"  File    : {fname}")
    print()

    # Save attachment to a temp file first so we don't clobber the dest on error
    tmp = os.path.join(tempfile.gettempdir(), "southport_vessel_lineup_tmp.xlsx")
    attachment.SaveAsFile(tmp)

    shutil.copy2(tmp, DEST_FILE)
    os.remove(tmp)
    print(f"Saved → {DEST_FILE}")

    # Git commit + push
    print("\nPushing to GitHub...")
    cmds = [
        ["git", "-C", REPO_DIR, "add",    "Vessel Lineup - US.xlsx"],
        ["git", "-C", REPO_DIR, "commit", "-m",
         f"Update vessel lineup from Southport email: {subject}",
         "--author", "Kolten Postin <275148418+koltenpostin93-blip@users.noreply.github.com>"],
        ["git", "-C", REPO_DIR, "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # "nothing to commit" is not a real error
            if "nothing to commit" in result.stdout + result.stderr:
                print("  (file unchanged — nothing new to commit)")
                break
            print(f"ERROR running: {' '.join(cmd)}")
            print(result.stderr or result.stdout)
            input("\nPress Enter to exit.")
            sys.exit(1)

    print("\nDone! Streamlit Cloud will refresh in ~1 minute.")
    input("\nPress Enter to exit.")


if __name__ == "__main__":
    main()
