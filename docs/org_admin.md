# Org Admin Guide

## Access Model

promop uses **explicit-only access** — there are no "open" organisations where any signed-up user can browse patient records.

Every user must be granted access through one of two mechanisms:

| Mechanism | What it does |
|-----------|-------------|
| `GroupAccess` | Directly grants a named user (Identity) a role within an org or patient group |
| `OrgTrust` | Grants access to all users whose email domain matches, or to all users who already belong to a trusted partner org |

If a user has neither a `GroupAccess` grant nor a matching `OrgTrust`, they see **nothing**.

---

## Role Definitions

| Role | Who assigns | What they can do |
|------|-------------|-----------------|
| `org_admin` | Staff or another org_admin | Invite users, manage trusts, view/cancel invitations, list access grants, edit org settings |
| `doctor` | Org admin or staff | Read and edit patient records within their assigned org/group |
| `navigator` | Org admin or staff | Read and edit patient records within their assigned group |

---

## Data Trusts

### Domain Trust
An org can declare that **any user with a specific email domain** is trusted:

```
OrgTrust(granting_org=<ABC Foundation>, trusted_domain='healthkey.ai')
```

Effect: any user whose email ends in `@healthkey.ai` can view ABC Foundation's patients.

### Org-to-Org Trust
An org can declare that **any user already granted access to a partner org** is also trusted:

```
OrgTrust(granting_org=<Hospital A>, trusted_org=<Research Consortium>)
```

Effect: any user with an active `GroupAccess` to Research Consortium can also view Hospital A's patients.

### Trust constraints
- A single `OrgTrust` row must specify **either** `trusted_org` OR `trusted_domain` — never both and never neither (enforced by a database check constraint).
- Trusts are created and removed by org admins or staff via the API.

---

## Demo Access

A shared demo account is provided for casual evaluators:

| Field | Value |
|-------|-------|
| Email | `random@healthkey.ai` |
| Password | `password123!` |
| Role | No `GroupAccess` grant |

Access works because ABC Foundation has a `healthkey.ai` domain trust. The demo user's email domain matches, so they can browse ABC Foundation's patients without an explicit grant.

To provision the demo account and trust on a new environment:
```bash
python manage.py setup_demo
```

This is idempotent — safe to run multiple times.

---

## Org Admin Permissions

An org admin (a user with a `GroupAccess` row where `role='org_admin'`) can:

- View their org's settings
- Edit org name and `is_active` flag (staff-only)
- Invite new users by email (creates an `OrgInvitation`)
- View pending, confirmed, expired, and cancelled invitations
- Cancel a pending invitation
- Add a domain trust or org-to-org trust for their org
- Remove a trust for their org
- View the list of active `GroupAccess` grants for their org
- Revoke a `GroupAccess` grant

An org admin **cannot**:
- Create a new org (staff only)
- Delete an org (staff only)
- Manage other orgs they are not an admin of
- Elevate their own role

---

## Staff Permissions

A user with `is_staff=True` has full CRUD access to all orgs:

- List all orgs
- Create a new org
- Edit any org's settings
- Delete any org
- All org admin actions on any org

Staff access is provisioned via:
```bash
python manage.py create_staff_user   # creates adam@healthkey.ai / 1database (is_staff=True)
```

---

## API Reference

All org management endpoints require authentication. The `{slug}` path parameter is the org's URL slug.

### Org CRUD

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/api/orgs/` | Staff or org_admin | List orgs (staff: all; org_admin: own) |
| `POST` | `/api/orgs/` | Staff only | Create org |
| `GET` | `/api/orgs/{slug}/` | Staff or org_admin | Org detail |
| `PATCH` | `/api/orgs/{slug}/` | Staff or org_admin | Update settings |
| `DELETE` | `/api/orgs/{slug}/` | Staff only | Delete org |

### Invitations

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `POST` | `/api/orgs/{slug}/invite/` | Staff or org_admin | Invite email to org |
| `GET` | `/api/orgs/{slug}/invitations/` | Staff or org_admin | List invitations |
| `DELETE` | `/api/orgs/{slug}/invitations/{id}/` | Staff or org_admin | Cancel invitation |
| `POST` | `/api/orgs/confirm-invitation/` | Public (no auth) | Confirm invitation by token |

Confirmation body: `{"token": "<64-char token>"}`

On success: creates a `GroupAccess` row for the invited email's `Identity`, returns `200 OK`.

### Trusts

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/api/orgs/{slug}/trusts/` | Staff or org_admin | List trusts |
| `POST` | `/api/orgs/{slug}/trusts/` | Staff or org_admin | Add org or domain trust |
| `DELETE` | `/api/orgs/{slug}/trusts/{id}/` | Staff or org_admin | Remove trust |

Trust creation body examples:
```json
{"trusted_domain": "partner-hospital.org"}
{"trusted_org": 42}
```

### Access Grants

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/api/orgs/{slug}/access/` | Staff or org_admin | List GroupAccess grants |
| `DELETE` | `/api/orgs/{slug}/access/{id}/` | Staff or org_admin | Revoke access grant |

---

## Data Model Summary

```
Organization
  ├── is_active (bool)
  ├── created_by (FK → Identity)
  ├── trusts_granted → OrgTrust[]
  ├── access_grants → GroupAccess[]
  └── invitations → OrgInvitation[]

OrgTrust
  ├── granting_org (FK → Organization)
  ├── trusted_org (FK → Organization, nullable)
  ├── trusted_domain (str, blank ok)
  └── granted_by (FK → Identity)

OrgInvitation
  ├── org (FK → Organization)
  ├── email
  ├── role (org_admin | doctor | navigator)
  ├── token (64-char unique)
  ├── status (pending | confirmed | expired | cancelled)
  ├── expires_at (7 days from creation)
  └── invited_by (FK → Identity)

GroupAccess
  ├── identity (FK → Identity)
  ├── org (FK → Organization, XOR group)
  ├── group (FK → PatientGroup, XOR org)
  ├── role
  └── expires_at
```
