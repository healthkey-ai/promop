# Org Disease Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `ProfessionalGroupAccess` → `GroupAccess`, add an `org_admin` role scoped to an org, a `get_visible_orgs` access helper, a `/api/stats/org-disease/` endpoint, and a `/stats` frontend page showing per-org disease counts.

**Architecture:** The rename is a single migration; existing `doctor`/`navigator` rows are untouched (they still have `group` set). A new `omop_core/services/access.py` module provides `get_visible_orgs(user)` used by the stats view. The frontend adds a `StatsPage` component and a nav link.

**Tech Stack:** Django 5, DRF, PostgreSQL, React 18, TypeScript, Tailwind, Vitest/Testing Library.

---

## File map

| File | Change |
|---|---|
| `omop_core/models.py` | Rename class, add `org` FK, update constraints and choices |
| `omop_core/migrations/0095_group_access.py` | Rename table, add org FK, update constraints |
| `omop_core/services/access.py` | New — `get_visible_orgs(user)` |
| `omop_core/authorization.py` | Update import: `ProfessionalGroupAccess` → `GroupAccess` |
| `patient_portal/api/views.py` | Update import + queryset scoping; add `org_disease_stats` view; add `is_staff` to `UserSerializer` |
| `patient_portal/api/serializers.py` | Add `is_staff` to `UserSerializer` fields |
| `patient_portal/api/urls.py` | Add `stats/org-disease/` URL |
| `patient_portal/api/lab_results/views.py` | Update import |
| `patient_portal/api/lab_results/tests.py` | Update import + class name |
| `patient_portal/tests.py` | Update import; add stats endpoint tests |
| `omop_core/tests.py` | Add `get_visible_orgs` unit tests |
| `frontend/src/types/patient.ts` | Add `is_staff?: boolean` to `User` interface |
| `frontend/src/components/Stats/StatsPage.tsx` | New — stats page component |
| `frontend/src/components/Stats/StatsPage.test.tsx` | New — component tests |
| `frontend/src/App.tsx` | Add `/stats` route |
| `frontend/src/components/Patient/PatientList.tsx` | Add Stats nav link |

---

## Task 1: Rename model and run migration

**Files:**
- Modify: `omop_core/models.py`
- Create: `omop_core/migrations/0095_group_access.py` (via makemigrations)

- [ ] **Step 1: Update the model in `omop_core/models.py`**

Replace the entire `ProfessionalGroupAccess` class with:

```python
class GroupAccess(models.Model):
    """Grants a professional (Identity) access to an org or a patient group."""
    ROLE_CHOICES = [
        ('org_admin', 'Org Admin'),
        ('doctor',    'Doctor'),
        ('navigator', 'Navigator'),
    ]
    identity = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='group_access_grants',
    )
    org = models.ForeignKey(
        'Organization', on_delete=models.CASCADE,
        null=True, blank=True, related_name='access_grants',
    )
    group = models.ForeignKey(
        PatientGroup, on_delete=models.CASCADE,
        null=True, blank=True, related_name='access_grants',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    expires_at = models.DateTimeField(null=True, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'group_access'
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(org__isnull=False, group__isnull=True) |
                    Q(org__isnull=True, group__isnull=False)
                ),
                name='group_access_org_xor_group',
            ),
            models.UniqueConstraint(
                fields=['identity', 'group'],
                condition=Q(group__isnull=False),
                name='uq_identity_group',
            ),
            models.UniqueConstraint(
                fields=['identity', 'org'],
                condition=Q(org__isnull=False),
                name='uq_identity_org',
            ),
        ]

    def __str__(self):
        scope = f"org={self.org_id}" if self.org_id else f"group={self.group_id}"
        return f"{self.identity} → {scope} ({self.role})"
```

- [ ] **Step 2: Generate the migration**

```bash
.venv/bin/python manage.py makemigrations omop_core --name group_access
```

Expected: creates `omop_core/migrations/0095_group_access.py`.

- [ ] **Step 3: Edit the generated migration to rename the table instead of drop/create**

Open the generated migration. Replace the `CreateModel` / `DeleteModel` operations with a `RenameModel` + `AddField` + `AlterField` + `AddConstraint` / `RemoveConstraint` sequence. The migration must preserve existing rows. Use this operations list:

```python
from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings
from django.db.models import Q

class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0094_concept_name_trigram_index'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Rename the model (renames the table professional_group_access → group_access)
        migrations.RenameModel(
            old_name='ProfessionalGroupAccess',
            new_name='GroupAccess',
        ),
        # 2. Make group nullable (was non-nullable)
        migrations.AlterField(
            model_name='groupaccess',
            name='group',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='access_grants',
                to='omop_core.patientgroup',
            ),
        ),
        # 3. Add org FK
        migrations.AddField(
            model_name='groupaccess',
            name='org',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='access_grants',
                to='omop_core.organization',
            ),
        ),
        # 4. Replace old unique constraint with the two partial ones + check constraint
        migrations.RemoveConstraint(
            model_name='groupaccess',
            name='uq_identity_group',
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.CheckConstraint(
                check=(
                    Q(org__isnull=False, group__isnull=True) |
                    Q(org__isnull=True, group__isnull=False)
                ),
                name='group_access_org_xor_group',
            ),
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.UniqueConstraint(
                fields=['identity', 'group'],
                condition=Q(group__isnull=False),
                name='uq_identity_group',
            ),
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.UniqueConstraint(
                fields=['identity', 'org'],
                condition=Q(org__isnull=False),
                name='uq_identity_org',
            ),
        ),
        # 5. Update role choices (Python-only, no DB op needed — AlterField for state)
        migrations.AlterField(
            model_name='groupaccess',
            name='role',
            field=models.CharField(
                choices=[
                    ('org_admin', 'Org Admin'),
                    ('doctor', 'Doctor'),
                    ('navigator', 'Navigator'),
                ],
                max_length=20,
            ),
        ),
    ]
```

- [ ] **Step 4: Apply the migration to local test DB**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py migrate omop_core
```

Expected: `OK` — no errors.

- [ ] **Step 5: Update all references to `ProfessionalGroupAccess` in non-migration files**

In each file, replace `ProfessionalGroupAccess` → `GroupAccess`:

- `omop_core/authorization.py` line 15: `from omop_core.models import ... ProfessionalGroupAccess ...` → `GroupAccess`; line 43 and 79: variable names using the old class
- `patient_portal/api/views.py` line 158: import; lines 174+ usage
- `patient_portal/api/lab_results/views.py`: import
- `patient_portal/api/lab_results/tests.py`: import + class name `ProfessionalGroupAccessTest` → `GroupAccessTest`
- `patient_portal/tests.py` line 1564: comment only — update for clarity

Run a grep to catch any remaining references:

```bash
grep -rn "ProfessionalGroupAccess" . --include="*.py" | grep -v migrations | grep -v ".pyc"
```

Expected: no output.

- [ ] **Step 6: Run the full backend test suite**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add omop_core/models.py omop_core/migrations/0095_group_access.py \
        omop_core/authorization.py patient_portal/api/views.py \
        patient_portal/api/lab_results/views.py \
        patient_portal/api/lab_results/tests.py patient_portal/tests.py
git commit -m "refactor: rename ProfessionalGroupAccess → GroupAccess, add org FK and org_admin role"
```

---

## Task 2: `get_visible_orgs` access helper

**Files:**
- Create: `omop_core/services/access.py`
- Modify: `omop_core/tests.py`

- [ ] **Step 1: Write the failing tests in `omop_core/tests.py`**

Add at the bottom of `omop_core/tests.py`:

```python
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from omop_core.models import Organization, PatientGroup, GroupAccess
from omop_core.services.access import get_visible_orgs
from patient_portal.models import Identity


class GetVisibleOrgsTest(TestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name='Org A', slug='org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b')
        self.group_a = PatientGroup.objects.create(
            organization=self.org_a, name='Group A', slug='group-a'
        )
        self.staff_user = Identity.objects.create_user(
            email='staff@test.com', password='x', is_staff=True
        )
        self.org_admin = Identity.objects.create_user(
            email='orgadmin@test.com', password='x'
        )
        self.doctor = Identity.objects.create_user(
            email='doctor@test.com', password='x'
        )
        self.nobody = Identity.objects.create_user(
            email='nobody@test.com', password='x'
        )
        GroupAccess.objects.create(
            identity=self.org_admin, org=self.org_a, role='org_admin'
        )
        GroupAccess.objects.create(
            identity=self.doctor, group=self.group_a, role='doctor'
        )

    def test_staff_sees_all_orgs(self):
        orgs = get_visible_orgs(self.staff_user)
        self.assertIn(self.org_a, orgs)
        self.assertIn(self.org_b, orgs)

    def test_org_admin_sees_their_org_only(self):
        orgs = list(get_visible_orgs(self.org_admin))
        self.assertIn(self.org_a, orgs)
        self.assertNotIn(self.org_b, orgs)

    def test_doctor_sees_org_of_their_group(self):
        orgs = list(get_visible_orgs(self.doctor))
        self.assertIn(self.org_a, orgs)
        self.assertNotIn(self.org_b, orgs)

    def test_user_with_no_grants_sees_nothing(self):
        orgs = list(get_visible_orgs(self.nobody))
        self.assertEqual(orgs, [])

    def test_expired_grant_excluded(self):
        expired = Identity.objects.create_user(email='expired@test.com', password='x')
        GroupAccess.objects.create(
            identity=expired, org=self.org_a, role='org_admin',
            expires_at=timezone.now() - timedelta(hours=1),
        )
        orgs = list(get_visible_orgs(expired))
        self.assertEqual(orgs, [])

    def test_active_grant_with_future_expiry_included(self):
        future = Identity.objects.create_user(email='future@test.com', password='x')
        GroupAccess.objects.create(
            identity=future, org=self.org_b, role='org_admin',
            expires_at=timezone.now() + timedelta(days=30),
        )
        orgs = list(get_visible_orgs(future))
        self.assertIn(self.org_b, orgs)
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test omop_core.tests.GetVisibleOrgsTest --verbosity=2 --noinput
```

Expected: `ImportError: cannot import name 'get_visible_orgs'`

- [ ] **Step 3: Create `omop_core/services/access.py`**

```python
from django.db.models import Q, QuerySet
from django.utils import timezone

from omop_core.models import GroupAccess, Organization


def get_visible_orgs(user) -> QuerySet:
    """Return the orgs whose patients the user is allowed to see.

    - is_staff → all orgs
    - org_admin grant → their org(s)
    - doctor/navigator grant → the org of each assigned group
    - no grants → empty queryset
    """
    if getattr(user, 'is_staff', False):
        return Organization.objects.all()

    now = timezone.now()
    active = GroupAccess.objects.filter(
        identity=user,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    org_ids = set(
        active.filter(role='org_admin')
              .values_list('org_id', flat=True)
    )
    group_org_ids = set(
        active.exclude(role='org_admin')
              .values_list('group__organization_id', flat=True)
    )
    all_ids = org_ids | group_org_ids

    if not all_ids:
        return Organization.objects.none()

    return Organization.objects.filter(id__in=all_ids)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test omop_core.tests.GetVisibleOrgsTest --verbosity=2 --noinput
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/access.py omop_core/tests.py
git commit -m "feat: add get_visible_orgs access helper and tests"
```

---

## Task 3: Stats API endpoint

**Files:**
- Modify: `patient_portal/api/views.py`
- Modify: `patient_portal/api/serializers.py`
- Modify: `patient_portal/api/urls.py`
- Modify: `patient_portal/tests.py`

- [ ] **Step 1: Write the failing tests in `patient_portal/tests.py`**

Add a new test class at the bottom of `patient_portal/tests.py`:

```python
from omop_core.models import Organization, PatientGroup, GroupAccess, PatientInfo, Person
from patient_portal.models import Identity
from rest_framework.test import APIClient
from rest_framework import status


class OrgDiseaseStatsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name='Org A', slug='org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b')
        self.group_a = PatientGroup.objects.create(
            organization=self.org_a, name='Group A', slug='group-a'
        )

        # Create patients
        for i, slug in enumerate(['mm', 'mm', 'breast-cancer'], start=1):
            p = Person.objects.create(person_id=9000 + i)
            PatientInfo.objects.create(person=p, organization=self.org_a, disease_slug=slug)

        p4 = Person.objects.create(person_id=9004)
        PatientInfo.objects.create(person=p4, organization=self.org_b, disease_slug='cll')

        self.staff = Identity.objects.create_user(email='staff@t.com', password='x', is_staff=True)
        self.org_admin = Identity.objects.create_user(email='admin@t.com', password='x')
        self.doctor = Identity.objects.create_user(email='doc@t.com', password='x')
        self.nobody = Identity.objects.create_user(email='none@t.com', password='x')

        GroupAccess.objects.create(identity=self.org_admin, org=self.org_a, role='org_admin')
        GroupAccess.objects.create(identity=self.doctor, group=self.group_a, role='doctor')

    def _get(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get('/api/stats/org-disease/')

    def test_staff_sees_all_orgs(self):
        resp = self._get(self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)
        self.assertIn('org-b', slugs)

    def test_staff_disease_counts_correct(self):
        resp = self._get(self.staff)
        org_a_data = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a_data['total'], 3)
        counts = {d['disease_slug']: d['count'] for d in org_a_data['disease_counts']}
        self.assertEqual(counts['mm'], 2)
        self.assertEqual(counts['breast-cancer'], 1)

    def test_org_admin_sees_only_their_org(self):
        resp = self._get(self.org_admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)
        self.assertNotIn('org-b', slugs)

    def test_doctor_sees_their_group_org(self):
        resp = self._get(self.doctor)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)

    def test_no_grants_returns_empty_list(self):
        resp = self._get(self.nobody)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_unauthenticated_returns_401(self):
        self.client.logout()
        resp = self.client.get('/api/stats/org-disease/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_response_shape(self):
        resp = self._get(self.org_admin)
        org = resp.data[0]
        self.assertIn('org_slug', org)
        self.assertIn('org_name', org)
        self.assertIn('total', org)
        self.assertIn('disease_counts', org)
        if org['disease_counts']:
            dc = org['disease_counts'][0]
            self.assertIn('disease_slug', dc)
            self.assertIn('label', dc)
            self.assertIn('count', dc)
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test patient_portal.tests.OrgDiseaseStatsTest --verbosity=2 --noinput
```

Expected: `404 Not Found` (URL doesn't exist yet).

- [ ] **Step 3: Add `is_staff` to `UserSerializer` in `patient_portal/api/serializers.py`**

Change:
```python
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = Identity
        fields = ['id', 'sub', 'email', 'name']
```

To:
```python
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = Identity
        fields = ['id', 'sub', 'email', 'name', 'is_staff']
```

- [ ] **Step 4: Add the `org_disease_stats` view to `patient_portal/api/views.py`**

Add this import near the top of views.py (alongside other service imports):

```python
from omop_core.services.access import get_visible_orgs
```

Add the view function — a good place is just before the `auth_test` function near the bottom of the file:

```python
# Disease label lookup — human-readable names for known disease slugs.
_DISEASE_LABELS = {
    'mm':                    'Multiple Myeloma',
    'MM':                    'Multiple Myeloma',
    'er-erbb2-breast-cancer': 'ER+/HER2+ Breast Cancer',
    'breast-cancer':          'Breast Cancer',
    'follicular-lymphoma':    'Follicular Lymphoma',
    'cll':                    'Chronic Lymphocytic Leukemia',
    'lung-cancer':            'Lung Cancer',
    'colon-cancer':           'Colon Cancer',
}


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def org_disease_stats(request):
    """GET /api/stats/org-disease/ — per-org disease patient counts for the requesting user."""
    from django.db.models import Count

    orgs = get_visible_orgs(request.user)
    result = []
    for org in orgs.order_by('name'):
        rows = (
            PatientInfo.objects
            .filter(organization=org)
            .values('disease_slug')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        disease_counts = [
            {
                'disease_slug': r['disease_slug'] or '',
                'label': _DISEASE_LABELS.get(r['disease_slug'] or '', r['disease_slug'] or 'Unknown'),
                'count': r['count'],
            }
            for r in rows
        ]
        result.append({
            'org_slug': org.slug,
            'org_name': org.name,
            'total': sum(d['count'] for d in disease_counts),
            'disease_counts': disease_counts,
        })
    return Response(result)
```

- [ ] **Step 5: Register the URL in `patient_portal/api/urls.py`**

Add alongside the other `path(...)` entries in `urlpatterns`:

```python
from patient_portal.api.views import org_disease_stats

# inside urlpatterns:
path('stats/org-disease/', org_disease_stats, name='stats-org-disease'),
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test patient_portal.tests.OrgDiseaseStatsTest --verbosity=2 --noinput
```

Expected: 7 tests pass.

- [ ] **Step 7: Run full backend suite**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/ctomop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add patient_portal/api/views.py patient_portal/api/serializers.py \
        patient_portal/api/urls.py patient_portal/tests.py
git commit -m "feat: add /api/stats/org-disease/ endpoint with role-based org scoping"
```

---

## Task 4: Frontend — StatsPage component

**Files:**
- Create: `frontend/src/components/Stats/StatsPage.tsx`
- Create: `frontend/src/components/Stats/StatsPage.test.tsx`
- Modify: `frontend/src/types/patient.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Patient/PatientList.tsx`

- [ ] **Step 1: Add `is_staff` to the `User` interface in `frontend/src/types/patient.ts`**

The file currently starts with:
```typescript
export interface User {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
}
```

Change to:
```typescript
export interface User {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_staff?: boolean;
}
```

- [ ] **Step 2: Write the failing frontend tests in `frontend/src/components/Stats/StatsPage.test.tsx`**

```typescript
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import StatsPage from './StatsPage';

vi.mock('@/api/axios', () => ({
  default: {
    get: vi.fn(),
  },
}));

import api from '@/api/axios';

const mockData = [
  {
    org_slug: 'abc-foundation',
    org_name: 'ABC Foundation',
    total: 5,
    disease_counts: [
      { disease_slug: 'mm', label: 'Multiple Myeloma', count: 3 },
      { disease_slug: 'breast-cancer', label: 'Breast Cancer', count: 2 },
    ],
  },
];

describe('StatsPage', () => {
  afterEach(() => vi.clearAllMocks());

  it('renders org cards with disease counts', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: mockData });
    render(<StatsPage />);
    await waitFor(() => expect(screen.getByText('ABC Foundation')).toBeInTheDocument());
    expect(screen.getByText('Multiple Myeloma')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('Breast Cancer')).toBeInTheDocument();
  });

  it('shows empty state when response is empty', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: [] });
    render(<StatsPage />);
    await waitFor(() =>
      expect(screen.getByText('No patient data available for your account.')).toBeInTheDocument()
    );
  });

  it('shows loading state before response resolves', () => {
    (api.get as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(() => {}));
    render(<StatsPage />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run to confirm tests fail**

```bash
cd frontend && npm test -- --run 2>&1 | grep -E "FAIL|cannot find|StatsPage"
```

Expected: errors about missing `StatsPage` module.

- [ ] **Step 4: Create `frontend/src/components/Stats/StatsPage.tsx`**

```typescript
import React, { useEffect, useState } from 'react';
import api from '@/api/axios';

interface DiseaseCount {
  disease_slug: string;
  label: string;
  count: number;
}

interface OrgStats {
  org_slug: string;
  org_name: string;
  total: number;
  disease_counts: DiseaseCount[];
}

export default function StatsPage() {
  const [data, setData] = useState<OrgStats[] | null>(null);

  useEffect(() => {
    api.get<OrgStats[]>('/api/stats/org-disease/').then(r => setData(r.data));
  }, []);

  if (data === null) {
    return (
      <div className="p-8 text-center text-gray-500">Loading…</div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="p-8 text-center text-gray-500">
        No patient data available for your account.
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-semibold text-gray-900">Patient Summary by Organization</h1>
      {data.map(org => (
        <div key={org.org_slug} className="bg-white rounded-lg border border-gray-200 shadow-sm">
          <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
            <h2 className="text-lg font-medium text-gray-900">{org.org_name}</h2>
            <span className="text-sm text-gray-500">{org.total} patients</span>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-6 py-2 font-medium text-gray-600">Disease</th>
                <th className="text-right px-6 py-2 font-medium text-gray-600">Patients</th>
              </tr>
            </thead>
            <tbody>
              {org.disease_counts.map(dc => (
                <tr key={dc.disease_slug} className="border-t border-gray-100">
                  <td className="px-6 py-2 text-gray-800">{dc.label}</td>
                  <td className="px-6 py-2 text-right text-gray-800">{dc.count}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-gray-200 bg-gray-50">
                <td className="px-6 py-2 font-medium text-gray-700">Total</td>
                <td className="px-6 py-2 text-right font-medium text-gray-700">{org.total}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: Run frontend tests to confirm they pass**

```bash
cd frontend && npm test -- --run
```

Expected: `StatsPage` tests pass.

- [ ] **Step 6: Add `/stats` route to `frontend/src/App.tsx`**

Add the import at the top alongside other page imports:

```typescript
import StatsPage from "@/components/Stats/StatsPage";
```

Add the route inside `<Routes>` before the catch-all `*` route:

```typescript
<Route
  path="/stats"
  element={
    currentUser ? <StatsPage /> : <Navigate to="/login" replace />
  }
/>
```

- [ ] **Step 7: Add a Stats nav link to `frontend/src/components/Patient/PatientList.tsx`**

Find the button row that has "Upload CSV" and "Upload FHIR". Add a Stats link before them (using `useNavigate` or a plain `<a>`). The file already imports from `react-router-dom`; add `useNavigate` if not already imported.

Find the section with upload buttons and add:

```typescript
import { useNavigate } from 'react-router-dom';

// inside the component:
const navigate = useNavigate();

// in the JSX, before the Upload buttons:
<button
  onClick={() => navigate('/stats')}
  className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
>
  Stats
</button>
```

- [ ] **Step 8: Run full frontend test suite**

```bash
cd frontend && npm test -- --run
```

Expected: all tests pass.

- [ ] **Step 9: Type-check and build**

```bash
cd frontend && npm run build
```

Expected: no TypeScript errors, build succeeds.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/types/patient.ts \
        frontend/src/components/Stats/ \
        frontend/src/App.tsx \
        frontend/src/components/Patient/PatientList.tsx
git commit -m "feat: add StatsPage with per-org disease count breakdown"
```

---

## Task 5: Apply migration to staging DB

- [ ] **Step 1: Apply migration to staging**

```bash
source .env
DATABASE_URL="$STAGING_DATABASE_URL" .venv/bin/python manage.py migrate omop_core
```

Expected: migration 0095 applied cleanly.

- [ ] **Step 2: Verify DB/model in sync on staging**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py shell -c "
from django.db import connection
cursor = connection.cursor()
cursor.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='group_access' ORDER BY column_name\")
print([r[0] for r in cursor.fetchall()])
"
```

Expected: list includes `group_id`, `org_id`, `role`, `expires_at`, `identity_id`, `granted_at`, `granted_by_id`.

- [ ] **Step 3: Push branch**

```bash
git push -u origin feature/org-disease-stats
```
