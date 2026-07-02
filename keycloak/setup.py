#!/usr/bin/env python3
"""
One-shot Keycloak setup: creates realm, roles, client, group hierarchy,
and role-to-group mappers for EKAP.

Run ONCE after Keycloak first starts (the keycloak-setup Docker service does this).
Safe to re-run — all creates are idempotent via GET-before-POST.
"""
import os
import sys
import time

import requests

KC_URL       = os.environ.get("KC_URL",       "http://keycloak:8080/auth")
ADMIN_USER   = os.environ.get("KC_ADMIN",     "admin")
ADMIN_PASS   = os.environ.get("KC_ADMIN_PASS","admin")
REALM        = os.environ.get("KC_REALM",     "ekap")
CLIENT_ID    = os.environ.get("KC_CLIENT_ID", "ekap-web")
REDIRECT_URI = os.environ.get("KC_REDIRECT_URI", "http://localhost/*")

ROLES = [
    ("user",                 "Standard read-only knowledge access"),
    ("power-user",           "Access to Confidential documents"),
    ("knowledge-manager",    "Upload, tag, and manage documents"),
    ("administrator",        "Full EKAP administration"),
    ("system-administrator", "Full system access including Restricted docs"),
]

GROUPS = [
    ("/ekap-users",           "user"),
    ("/ekap-power-users",     "power-user"),
    ("/ekap-knowledge-mgrs",  "knowledge-manager"),
    ("/ekap-admins",          "administrator"),
    ("/ekap-sysadmins",       "system-administrator"),
]


# ── Admin token ────────────────────────────────────────────────────────────────

def get_admin_token() -> str:
    resp = requests.post(
        f"{KC_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id":  "admin-cli",
            "username":   ADMIN_USER,
            "password":   ADMIN_PASS,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Idempotent helpers ─────────────────────────────────────────────────────────

def create_realm(token: str):
    resp = requests.get(f"{KC_URL}/admin/realms/{REALM}", headers=headers(token))
    if resp.status_code == 200:
        print(f"  [exists] realm '{REALM}'")
        return
    r = requests.post(f"{KC_URL}/admin/realms", headers=headers(token), json={
        "realm": REALM,
        "enabled": True,
        "displayName": "EKAP",
        "registrationAllowed": False,
        "resetPasswordAllowed": True,
        "bruteForceProtected": True,
        "accessTokenLifespan": 3600,
        "ssoSessionMaxLifespan": 36000,
    })
    r.raise_for_status()
    print(f"  [created] realm '{REALM}'")


def create_roles(token: str):
    for name, description in ROLES:
        check = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/roles/{name}", headers=headers(token)
        )
        if check.status_code == 200:
            print(f"  [exists] role '{name}'")
            continue
        r = requests.post(
            f"{KC_URL}/admin/realms/{REALM}/roles", headers=headers(token),
            json={"name": name, "description": description}
        )
        r.raise_for_status()
        print(f"  [created] role '{name}'")


def get_role(token: str, name: str) -> dict:
    r = requests.get(f"{KC_URL}/admin/realms/{REALM}/roles/{name}", headers=headers(token))
    r.raise_for_status()
    return r.json()


def create_groups(token: str) -> dict[str, str]:
    """Create groups and return {group_path: group_id}."""
    existing = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/groups?briefRepresentation=true&max=50",
        headers=headers(token)
    ).json()
    existing_names = {g["name"]: g["id"] for g in existing}

    group_ids: dict[str, str] = {}
    for path, role_name in GROUPS:
        name = path.lstrip("/")
        if name in existing_names:
            print(f"  [exists] group '{path}'")
            group_ids[path] = existing_names[name]
            continue
        r = requests.post(
            f"{KC_URL}/admin/realms/{REALM}/groups", headers=headers(token),
            json={"name": name}
        )
        r.raise_for_status()
        # Fetch the newly created group's ID
        refreshed = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/groups?briefRepresentation=true&max=50",
            headers=headers(token)
        ).json()
        gid = next(g["id"] for g in refreshed if g["name"] == name)
        group_ids[path] = gid
        print(f"  [created] group '{path}' id={gid}")
    return group_ids


def assign_group_roles(token: str, group_ids: dict[str, str]):
    for path, role_name in GROUPS:
        gid  = group_ids[path]
        role = get_role(token, role_name)
        r    = requests.post(
            f"{KC_URL}/admin/realms/{REALM}/groups/{gid}/role-mappings/realm",
            headers=headers(token), json=[role]
        )
        if r.status_code in (204, 200):
            print(f"  [mapped] group '{path}' → role '{role_name}'")
        elif r.status_code == 409:
            print(f"  [exists] mapping '{path}' → '{role_name}'")
        else:
            r.raise_for_status()


def create_client(token: str):
    existing = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/clients?clientId={CLIENT_ID}",
        headers=headers(token)
    ).json()
    if existing:
        print(f"  [exists] client '{CLIENT_ID}'")
        return existing[0]["id"]

    r = requests.post(
        f"{KC_URL}/admin/realms/{REALM}/clients", headers=headers(token), json={
            "clientId":                  CLIENT_ID,
            "enabled":                   True,
            "publicClient":              True,
            "standardFlowEnabled":       True,
            "implicitFlowEnabled":       False,
            "directAccessGrantsEnabled": True,
            "redirectUris":              [REDIRECT_URI, "http://localhost:3000/*"],
            "webOrigins":                ["+"],
            "protocol":                  "openid-connect",
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris":  REDIRECT_URI,
            },
        }
    )
    r.raise_for_status()
    refreshed = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/clients?clientId={CLIENT_ID}",
        headers=headers(token)
    ).json()
    cid = refreshed[0]["id"]
    print(f"  [created] client '{CLIENT_ID}' id={cid}")
    return cid


def add_groups_mapper(token: str, client_uuid: str):
    """Add a mapper so 'groups' claim appears in the access token."""
    mappers = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/clients/{client_uuid}/protocol-mappers/models",
        headers=headers(token)
    ).json()
    if any(m.get("name") == "groups" for m in mappers):
        print("  [exists] groups mapper")
        return
    r = requests.post(
        f"{KC_URL}/admin/realms/{REALM}/clients/{client_uuid}/protocol-mappers/models",
        headers=headers(token), json={
            "name":           "groups",
            "protocol":       "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {
                "full.path":            "false",
                "id.token.claim":       "true",
                "access.token.claim":   "true",
                "claim.name":           "groups",
                "userinfo.token.claim": "true",
            },
        }
    )
    r.raise_for_status()
    print("  [created] groups mapper")


def create_default_admin_user(token: str):
    """Create an initial admin user so the system is usable out-of-the-box."""
    admin_email = "admin@ekap.local"
    existing = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/users?email={admin_email}",
        headers=headers(token)
    ).json()
    if existing:
        print(f"  [exists] user '{admin_email}'")
        uid = existing[0]["id"]
    else:
        r = requests.post(
            f"{KC_URL}/admin/realms/{REALM}/users", headers=headers(token), json={
                "username":    "ekap-admin",
                "email":       admin_email,
                "enabled":     True,
                "firstName":   "EKAP",
                "lastName":    "Admin",
                "credentials": [{"type": "password", "value": "Admin1234!", "temporary": True}],
            }
        )
        r.raise_for_status()
        uid = requests.get(
            f"{KC_URL}/admin/realms/{REALM}/users?email={admin_email}",
            headers=headers(token)
        ).json()[0]["id"]
        print(f"  [created] user 'ekap-admin' id={uid}")

    # Assign to sysadmin group
    groups_resp = requests.get(
        f"{KC_URL}/admin/realms/{REALM}/groups?briefRepresentation=true&max=50",
        headers=headers(token)
    ).json()
    sysadmin_group = next(
        (g for g in groups_resp if g["name"] == "ekap-sysadmins"), None
    )
    if sysadmin_group:
        r = requests.put(
            f"{KC_URL}/admin/realms/{REALM}/users/{uid}/groups/{sysadmin_group['id']}",
            headers=headers(token)
        )
        if r.status_code in (200, 204):
            print(f"  [mapped] ekap-admin → ekap-sysadmins group")


# ── Main ───────────────────────────────────────────────────────────────────────

def wait_for_keycloak(max_wait: int = 120):
    print("Waiting for Keycloak to be ready...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(f"{KC_URL}/realms/master", timeout=5)
            if r.status_code == 200:
                print("Keycloak is ready.")
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(5)
    print("ERROR: Keycloak did not become ready in time.", file=sys.stderr)
    sys.exit(1)


def main():
    wait_for_keycloak()
    print("\n=== EKAP Keycloak Setup ===\n")

    token = get_admin_token()

    print("Creating realm...")
    create_realm(token)

    # Token may expire after realm create — refresh
    token = get_admin_token()

    print("Creating roles...")
    create_roles(token)

    print("Creating groups...")
    group_ids = create_groups(token)

    print("Assigning roles to groups...")
    assign_group_roles(token, group_ids)

    print("Creating OIDC client...")
    client_uuid = create_client(token)

    print("Adding groups mapper...")
    add_groups_mapper(token, client_uuid)

    print("Creating default admin user...")
    create_default_admin_user(token)

    print("\n=== Setup complete ===")
    print(f"  Realm:       {REALM}")
    print(f"  Client ID:   {CLIENT_ID}")
    print(f"  Admin user:  ekap-admin / Admin1234! (change on first login)")
    print(f"  Keycloak UI: {KC_URL}/admin/master/console/")


if __name__ == "__main__":
    main()
