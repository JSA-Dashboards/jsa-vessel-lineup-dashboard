#!/usr/bin/env python3
"""
setup_poller_auth.py -- run ONCE interactively on the droplet.

Does a device-code login with the basis-tracker's AZURE_CLIENT_ID
(which already has Mail.Read) and saves the token to a cache file
that southport_poller.py will use for unattended silent refresh.

Run:
    /opt/vessel-lineup-dashboard/.venv/bin/python3 setup_poller_auth.py
"""

from pathlib import Path
import msal

ENV_FILE   = Path("/opt/basis-tracker/.env")
CACHE_FILE = Path("/opt/vessel-lineup-dashboard/.token_cache.json")
SCOPES     = ["Mail.Read"]


def parse_env(path):
    out = {}
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


env       = parse_env(ENV_FILE)
client_id = env["AZURE_CLIENT_ID"]
tenant_id = env.get("GRAPH_TENANT_ID") or env.get("AZURE_TENANT_ID", "common")

cache = msal.SerializableTokenCache()
if CACHE_FILE.exists():
    cache.deserialize(CACHE_FILE.read_text())

app = msal.PublicClientApplication(
    client_id=client_id,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
    token_cache=cache,
)

# Try silent first in case cache already has a valid token
accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

if not result:
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow}")

    print("\n" + "=" * 60)
    print(flow["message"])
    print("=" * 60 + "\n")
    result = app.acquire_token_by_device_flow(flow)

if "access_token" not in result:
    raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")

CACHE_FILE.write_text(cache.serialize())
print(f"\nToken saved to {CACHE_FILE}")
print("southport_poller.py will now use silent refresh automatically.")
