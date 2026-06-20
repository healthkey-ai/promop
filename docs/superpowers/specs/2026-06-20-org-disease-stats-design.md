# Org Disease Stats — Design Spec

**Date:** 2026-06-20
**Branch:** feature/org-disease-stats
**Status:** Draft

---

## Problem

There is no way for a logged-in user to see a summary of the patients they have access to. Admins and clinicians need a quick per-org breakdown of disease counts without duplicating the full analytics available in `~/analytics`.

Additionally, the existing `ProfessionalGroupAccess` model only supports group-scoped roles (`doctor`, `navigator`). There is no role that grants access to an entire org's patient population without needing to enumerate every group within it.

---

## Roles

### Existing: `Identity.is_staff` — global access

No changes. Staff users already bypass org scoping in `get_request_org` and `ScopedTokenPermission`. The stats endpoint will treat `is_staff=True` as "see all orgs."

### New: `org_admin` via renamed `GroupAccess` model

**Rename `ProfessionalGroupAccess` → `GroupAccess`** (table: `professional_group_access` → `group_access`).

Updated `ROLE_CHOICES`:

| Role | Scope field | Access |
|---|---|---|
| `org_admin` | `org` (non-null), `group` (null) | All patients in one org |
| `doctor` | `group` (non-null), `org` (null) | Patients in assigned group |
| `navigator` | `group` (non-null), `org` (null) | Patients in assigned group |

`doctor` and `navigator` have identical access for now — the distinction is label-only.

### DB constraint

A `CheckConstraint` enforces that exactly one of `org` or `group` is set:

```python
models.CheckConstraint(
    check=(
        Q(org__isnull=False, group__isnull=True) |
        Q(org__isnull=True, group__isnull=False)
    ),
    name='group_access_org_xor_group',
)
```

The unique constraint is updated to cover both cases:
- `UniqueConstraint(fields=['identity', 'group'], condition=Q(group__isnull=False), name='uq_identity_group')`
- `UniqueConstraint(fields=['identity', 'org'], condition=Q(org__isnull=False), name='uq_identity_org')`

---

## Backend

### Model changes (`omop_core/models.py`)

```python
class GroupAccess(models.Model):
    ROLE_CHOICES = [
        ('org_admin', 'Org Admin'),
        ('doctor',    'Doctor'),
        ('navigator', 'Navigator'),
    ]
    identity   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='group_access_grants')
    org        = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='access_grants')
    group      = models.ForeignKey(PatientGroup,  on_delete=models.CASCADE, null=True, blank=True, related_name='access_grants')
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES)
    expires_at = models.DateTimeField(null=True, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'group_access'
        constraints = [
            models.CheckConstraint(
                check=Q(org__isnull=False, group__isnull=True) | Q(org__isnull=True, group__isnull=False),
                name='group_access_org_xor_group',
            ),
            models.UniqueConstraint(fields=['identity', 'group'], condition=Q(group__isnull=False), name='uq_identity_group'),
            models.UniqueConstraint(fields=['identity', 'org'],   condition=Q(org__isnull=False),  name='uq_identity_org'),
        ]
```

### Migration

Single migration: rename table, add `org` FK, add `CheckConstraint`, update unique constraints, add `org_admin` to choices (choices are Python-only — no DB change needed for that).

The existing `uq_identity_group` constraint is replaced; data migration is a no-op since all existing rows have `group` set (the old non-nullable FK) and `org=null`.

### Stats endpoint

**`GET /api/stats/org-disease/`**

Returns a list of orgs the requesting user can see, each with a `disease_counts` dict.

**Access logic** (in `patient_portal/api/views.py` or a new `stats.py`):

```
if user.is_staff:
    orgs = Organization.objects.all()
elif user has org_admin GroupAccess rows:
    orgs = orgs from those rows (non-expired)
elif user has doctor/navigator GroupAccess rows:
    orgs = orgs of the PatientGroups in those rows (non-expired)
else:
    return []
```

For each org, query:

```python
PatientInfo.objects
    .filter(organization=org)
    .values('disease_slug')
    .annotate(count=Count('id'))
    .order_by('-count')
```

**Response shape:**

```json
[
  {
    "org_slug": "abc-foundation",
    "org_name": "ABC Foundation",
    "total": 123,
    "disease_counts": [
      {"disease_slug": "er-erbb2-breast-cancer", "label": "ER+/HER2+ Breast Cancer", "count": 123}
    ]
  }
]
```

`label` is a human-readable mapping from `disease_slug` (derived in the view; falls back to the slug if unmapped).

**Auth:** Session auth or OAuth token. No org scoping via `get_request_org` — the endpoint has its own access logic above.

**URL:** `path('stats/org-disease/', org_disease_stats, name='stats-org-disease')` in `patient_portal/api/urls.py`.

---

## Frontend

### Route

`/stats` added to `App.tsx`, protected by `currentUser` check (same pattern as existing routes).

### `StatsPage` component (`frontend/src/components/Stats/StatsPage.tsx`)

- Fetches `GET /api/stats/org-disease/` on mount via axios
- Renders one card per org
- Each card: org name as heading, table of disease label + count, total at the bottom
- Empty state: "No patient data available for your account"
- Loading state: skeleton or spinner

### Nav link

Add a "Stats" link to the top nav in `PatientList.tsx` (where Upload CSV / Upload FHIR buttons live), visible to all logged-in users. If the endpoint returns an empty list, the page shows the empty state gracefully.

### `User` type update

The `User` interface in `frontend/src/types/patient.ts` gains `is_staff?: boolean` so the frontend can conditionally show/hide admin-only UI in the future. The `/api/auth/test/` or equivalent endpoint must return `is_staff` — check and add if missing.

---

## Access logic helper

Extract into `omop_core/services/access.py`:

```python
def get_visible_orgs(user) -> QuerySet[Organization]:
    """Return the orgs whose patients the user can see."""
    if user.is_staff:
        return Organization.objects.all()
    now = timezone.now()
    active = GroupAccess.objects.filter(
        identity=user,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    org_ids = set(active.filter(role='org_admin').values_list('org_id', flat=True))
    group_org_ids = set(
        active.exclude(role='org_admin')
              .values_list('group__organization_id', flat=True)
    )
    return Organization.objects.filter(id__in=org_ids | group_org_ids)
```

The stats endpoint calls `get_visible_orgs(request.user)` rather than inlining the logic.

---

## Testing

### Backend (`patient_portal/tests.py`)

- Staff user sees all orgs in the response
- `org_admin` user sees only their org
- `doctor`/`navigator` user sees the org of their assigned group
- User with no `GroupAccess` rows gets an empty list
- Expired `GroupAccess` rows are excluded
- `CheckConstraint` prevents a row with both `org` and `group` set (DB-level test)

### Frontend (`StatsPage.test.tsx`)

- Renders org cards from mocked API response
- Shows empty state when response is `[]`
- Shows loading state before response resolves

---

## Out of scope

- Editing `GroupAccess` rows via the UI (admin-only, managed via Django admin or management command for now)
- Per-group breakdown within an org
- Filtering/sorting on the stats page
- `~/analytics` feature parity
