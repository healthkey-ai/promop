from rest_framework import serializers
from patient_portal.models import Identity, PatientConsent, PatientMessage
from omop_core.models import (
    PatientRecord, Concept, FieldConceptMapping, FieldSynonym, Person,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    PatientDocument, PatientTrialEnrollment, ProvenanceRecord,
    Survey, PatientSurveyResponse,
    StemCellTransplant, SctEligibility, PostTransformationOutcome,
    Organization, OrgTrust, OrgInvitation, GroupAccess,
    InterchangeAgreement,
)
from omop_oncology.models import Episode, EpisodeEvent
from datetime import date
from django.utils.timezone import localdate
from django.utils import timezone
from omop_core.services.access import has_org_admin_access
from omop_core.services.patient_record_service import PATIENT_RECORD_OMOP_MAPPED_FIELDS


class UserSerializer(serializers.ModelSerializer):
    is_org_admin = serializers.SerializerMethodField()
    org_accesses = serializers.SerializerMethodField()
    is_patient = serializers.SerializerMethodField()
    person_id = serializers.SerializerMethodField()

    class Meta:
        model = Identity
        fields = [
            'id', 'sub', 'email', 'name', 'is_staff', 'is_superuser',
            'is_org_admin', 'org_accesses', 'is_patient', 'person_id',
            'must_change_password',
        ]
        read_only_fields = ['must_change_password']

    def _patient_person(self, obj):
        """Memoized patient-record lookup so is_patient/person_id share one query."""
        cache = getattr(self, '_patient_person_cache', None)
        if cache is None:
            cache = self._patient_person_cache = {}
        if obj.pk not in cache:
            from patient_portal.services import patient_person_for
            cache[obj.pk] = patient_person_for(obj)
        return cache[obj.pk]

    def get_is_patient(self, obj):
        """True when this identity is a PHR Account Holder (patient). See PH.1."""
        return self._patient_person(obj) is not None

    def get_person_id(self, obj):
        """The person_id of the patient's own record, or None for non-patients."""
        person = self._patient_person(obj)
        return person.person_id if person else None

    def get_is_org_admin(self, obj):
        return has_org_admin_access(obj)

    def get_org_accesses(self, obj):
        now = timezone.now()
        from django.db.models import Q
        grants = GroupAccess.objects.filter(
            identity=obj,
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        ).select_related('org', 'group__organization').order_by('role')
        result = []
        for g in grants:
            if g.org:
                result.append({'org_name': g.org.name, 'org_slug': g.org.slug, 'role': g.role, 'expires_at': g.expires_at})
            elif g.group and g.group.organization:
                result.append({'org_name': g.group.organization.name, 'org_slug': g.group.organization.slug, 'role': g.role, 'expires_at': g.expires_at})
        return result


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'is_active', 'allows_public_aggregated_data', 'allows_patient_signup', 'clinical_unit_system', 'created_at']
        read_only_fields = ['id', 'created_at']


class PatientInvitationSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)

    class Meta:
        from patient_portal.models import PatientInvitation
        model = PatientInvitation
        fields = ['id', 'person_id', 'email', 'status', 'created_at', 'expires_at', 'accepted_at']
        read_only_fields = fields


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        from patient_portal.models import AuditEvent
        model = AuditEvent
        fields = [
            'id', 'event_type', 'timestamp', 'method', 'path', 'status_code',
            'user_id', 'user_email', 'client_id', 'resource_id', 'ip_address',
            'duration_ms', 'detail',
        ]
        read_only_fields = fields


class OrgTrustSerializer(serializers.ModelSerializer):
    granting_org_slug = serializers.SlugRelatedField(
        source='granting_org', slug_field='slug', read_only=True,
    )
    # Write field: accepts an org PK when creating a trust
    trusted_org = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )
    # Read field: exposes the trusted org's slug in responses
    trusted_org_slug = serializers.SlugRelatedField(
        source='trusted_org', slug_field='slug', read_only=True, allow_null=True,
    )

    class Meta:
        model = OrgTrust
        fields = [
            'id', 'granting_org_slug',
            'trusted_org', 'trusted_org_slug',
            'trusted_domain', 'created_at',
        ]
        read_only_fields = ['id', 'granting_org_slug', 'trusted_org_slug', 'created_at']

    def validate(self, data):
        trusted_org = data.get('trusted_org')
        trusted_domain = data.get('trusted_domain', '')
        if trusted_org and trusted_domain:
            raise serializers.ValidationError(
                'Specify either trusted_org or trusted_domain, not both.'
            )
        if not trusted_org and not trusted_domain:
            raise serializers.ValidationError(
                'Specify either trusted_org or trusted_domain.'
            )
        return data


class OrgInvitationSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    org_slug = serializers.SlugRelatedField(source='org', slug_field='slug', read_only=True)
    redirect_url = serializers.SerializerMethodField()
    person_id = serializers.IntegerField(source='person.person_id', read_only=True, default=None)

    class Meta:
        model = OrgInvitation
        fields = [
            'id', 'org_slug', 'email', 'role', 'redirect_url', 'person_id', 'status',
            'expires_at', 'created_at',
        ]
        read_only_fields = ['id', 'org_slug', 'redirect_url', 'person_id', 'status', 'expires_at', 'created_at']

    def get_status(self, obj):
        return obj.status

    def get_redirect_url(self, obj):
        return obj.redirect_url or None


class GroupAccessSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='identity.email', read_only=True)
    name = serializers.CharField(source='identity.name', read_only=True, default='')
    is_premium = serializers.BooleanField(source='identity.is_premium', read_only=True)
    org_slug = serializers.SlugRelatedField(source='org', slug_field='slug', read_only=True)
    group_name = serializers.CharField(source='group.name', read_only=True, default=None)
    redirect_url = serializers.SerializerMethodField()

    class Meta:
        model = GroupAccess
        fields = [
            'id', 'email', 'name', 'is_premium', 'org_slug', 'group_name', 'role',
            'redirect_url', 'expires_at', 'granted_at',
        ]
        read_only_fields = [
            'id', 'email', 'name', 'is_premium', 'org_slug', 'group_name', 'role',
            'redirect_url', 'expires_at', 'granted_at',
        ]

    def get_redirect_url(self, obj):
        return obj.redirect_url or None


class PatientListSerializer(serializers.ModelSerializer):
    """Serializer for patient list view with key fields"""
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)
    patient_name = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    organization_name = serializers.CharField(source='organization.name', read_only=True, allow_null=True)
    organization_slug = serializers.CharField(source='organization.slug', read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(format='%Y-%m-%d', read_only=True)
    
    class Meta:
        model = PatientRecord
        fields = [
            'id',
            'person_id',
            'patient_name',
            'age',
            'organization_name',
            'organization_slug',
            'disease',
            'stage',
            'updated_at',
        ]
    
    def get_patient_name(self, obj):
        # Get name from Person model (OMOP extension)
        if obj.person:
            full_name = f"{obj.person.given_name or ''} {obj.person.family_name or ''}".strip()
            return full_name if full_name else f"Patient {obj.person.person_id}"
        return "Unknown Patient"

    def get_age(self, obj):
        if obj.date_of_birth:
            today = date.today()
            age = today.year - obj.date_of_birth.year - ((today.month, today.day) < (obj.date_of_birth.month, obj.date_of_birth.day))
            return age
        return None


class GenderField(serializers.CharField):
    """Translates between display values (Male/Female) and DB codes (M/F)."""
    DISPLAY_TO_CODE = {'Male': 'M', 'Female': 'F', 'Other': '', 'Unknown': ''}
    CODE_TO_DISPLAY = {'M': 'Male', 'F': 'Female'}

    def to_representation(self, value):
        return self.CODE_TO_DISPLAY.get(value, 'Unknown')

    def to_internal_value(self, data):
        title = str(data).title()
        return self.DISPLAY_TO_CODE.get(title, data)


def _derived_wearable_fields():
    """Every wearable summary column on PatientRecord, read off the model.

    These are written only by refresh_patient_record, deriving them from OMOP
    measurement/observation rows. A client PATCH must never set one: the value
    would survive until the next refresh recomputed it, and during that window
    the column disagrees with the OMOP rows it claims to summarize, with no
    indication that it does. The window is unbounded in practice — refresh
    fires on OMOP writes for that person, so a patient who stops syncing their
    device never triggers one.

    This is computed rather than hand-listed because the hand-listed version
    drifted: ten columns were protected, and the eleven added afterwards were
    not (#440). Enumerating the model means a new column is protected the day
    it is added, rather than the day someone notices.

    Matching on the naming convention deliberately errs toward
    over-protection. A future settings field that happened to match would be
    wrongly read-only — which surfaces immediately as a rejected write. The
    opposite failure, a derived column silently accepting client values, is
    invisible and is exactly what this function exists to prevent.
    """
    return tuple(
        field.name
        for field in PatientRecord._meta.get_fields()
        if getattr(field, 'concrete', False)
        and (field.name.endswith('_30d') or field.name.startswith('wearable_'))
    )


class PatientRecordSerializer(serializers.ModelSerializer):
    person_id = serializers.IntegerField(source='person.person_id', read_only=True)
    patient_name = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    age = serializers.SerializerMethodField()
    gender = GenderField(read_only=True)
    refractory_status = serializers.CharField(source='treatment_refractory_status', read_only=True)
    first_line_therapy_display = serializers.SerializerMethodField()
    second_line_therapy_display = serializers.SerializerMethodField()
    later_therapy_display = serializers.SerializerMethodField()
    lines_of_therapy = serializers.SerializerMethodField()
    therapy_release_id = serializers.SerializerMethodField()

    class Meta:
        model = PatientRecord
        fields = '__all__'
        # organization and person must never be client-writable: they are
        # set server-side from the auth token / FHIR upload respectively.
        # A client supplying either field in a PATCH would bypass tenant
        # isolation (organization) or reassign the record to another patient.
        read_only_fields = (
            'organization', 'person', 'created_at', 'updated_at',
            'first_line_therapy_display', 'second_line_therapy_display', 'later_therapy_display',
            'lines_of_therapy', 'therapy_release_id',
            'death_date',
            # Derivation versioning — set only by refresh_patient_record, never by client.
            'derivation_version', 'derived_at',
            # Internal migration bookkeeping; clients must not set it.
            'user_edited_fields',
        ) + tuple(sorted(PATIENT_RECORD_OMOP_MAPPED_FIELDS))

    def get_patient_name(self, obj):
        if obj.person:
            full_name = f"{obj.person.given_name or ''} {obj.person.family_name or ''}".strip()
            return full_name if full_name else f"Patient {obj.person.person_id}"
        return f"Patient {obj.pk}"

    def get_name(self, obj):
        return self.get_patient_name(obj)

    def to_representation(self, instance):
        # Bulk-fetch all Concept rows referenced by therapy_id fields in one query,
        # replacing the per-field Concept.objects.filter() calls in the display methods.
        # NOTE: this fires one DB query per instance — do NOT use PatientRecordSerializer
        # in list views (many=True) without pre-fetching therapy_id concepts, as it
        # will produce N queries for N patients. Use PatientListSerializer for lists.
        concept_ids = set()
        if instance.first_line_therapy_id:
            concept_ids.add(instance.first_line_therapy_id)
        if instance.second_line_therapy_id:
            concept_ids.add(instance.second_line_therapy_id)
        concept_ids.update(instance.later_therapy_ids or [])
        self._therapy_concept_cache = (
            {c.concept_id: c for c in Concept.objects.filter(concept_id__in=concept_ids).only('concept_id', 'concept_name')}
            if concept_ids else {}
        )
        data = super().to_representation(instance)
        return self._apply_demographic_redaction(instance, data)

    # PHR-S FM PH.1.2#05 — consent/preference-driven demographic rendering.
    # Fields suppressed for non-owner readers when the patient has opted in via
    # PatientRecord.suppress_demographics_for_others.
    REDACTED_DEMOGRAPHIC_FIELDS = (
        'date_of_birth', 'age', 'patient_age',
        'country', 'region', 'city', 'postal_code', 'longitude', 'latitude',
        'patient_name', 'name',
    )

    def _apply_demographic_redaction(self, instance, data):
        """Redact selected demographics unless the reader is the account holder.

        Bounded minimal hook (issue #307). Requires a ``request`` in serializer
        context to identify the reader; when absent (internal/derivation code
        paths) no redaction is applied. Wiring redaction into every read path
        that omits serializer context is documented as deferred.
        """
        if not getattr(instance, 'suppress_demographics_for_others', False):
            return data
        request = self.context.get('request')
        if request is None or self._is_account_holder(request, instance):
            return data
        for field in self.REDACTED_DEMOGRAPHIC_FIELDS:
            if field in data:
                data[field] = None
        data['demographics_redacted'] = True
        return data

    @staticmethod
    def _is_account_holder(request, instance):
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return False
        from patient_portal.models import PatientUser
        return PatientUser.objects.filter(
            identity=user, person_id=instance.person_id,
        ).exists()

    def get_age(self, obj):
        if obj.date_of_birth:
            today = date.today()
            age = today.year - obj.date_of_birth.year - ((today.month, today.day) < (obj.date_of_birth.month, obj.date_of_birth.day))
            return age
        return None

    def get_first_line_therapy_display(self, obj):
        if obj.first_line_therapy_id:
            cache = getattr(self, '_therapy_concept_cache', {})
            c = cache.get(obj.first_line_therapy_id)
            return c.concept_name if c else obj.first_line_therapy
        return obj.first_line_therapy

    def get_second_line_therapy_display(self, obj):
        if obj.second_line_therapy_id:
            cache = getattr(self, '_therapy_concept_cache', {})
            c = cache.get(obj.second_line_therapy_id)
            return c.concept_name if c else obj.second_line_therapy
        return obj.second_line_therapy

    def get_later_therapy_display(self, obj):
        ids = obj.later_therapy_ids or []
        if not ids:
            return None
        cache = getattr(self, '_therapy_concept_cache', {})
        names = []
        for cid in ids:
            c = cache.get(cid)
            names.append(c.concept_name if c else str(cid))
        return names

    def get_lines_of_therapy(self, obj):
        """Structured per-line therapy history assembled from the flat
        first/second/later_* read-model fields (promop#249).

        Each entry: line number, regimen text + HemOnc concept_id (with its
        `regimen_source` and `release_id` from `therapy_ids_provenance`, falling
        back to `regimen_source='inferred'` for a resolved concept_id until
        provenance is recorded), component concept_ids, dates (ISO strings),
        outcome, intent, and discontinuation reason. 3L+ lines are emitted
        one-per `later_therapies` entry (the authoritative per-line list, which
        includes lines whose regimen did not resolve to a concept_id), each
        naming its own regimen and carrying its own concept_id (may be null) and
        dates; the `component_ids`/outcome remain the aggregate `later_*` values
        (not per-later-line), flagged with `later_aggregate: true`. Older records
        derived before `later_therapies` carried per-line concept_ids fall back
        to one entry per resolved `later_therapy_ids` id.
        """
        prov = obj.therapy_ids_provenance if isinstance(obj.therapy_ids_provenance, dict) else {}

        def _prov(field, key):
            p = prov.get(field)
            return p.get(key) if isinstance(p, dict) else None

        def _iso(v):
            # DateField values load from the DB as date objects, but an
            # in-memory instance may carry a raw string (Django does not coerce
            # on attribute assignment). Emit ISO either way, never crash.
            return v.isoformat() if hasattr(v, 'isoformat') else (v or None)

        def _line(n, regimen, cid, prov_field, comp, start, end,
                  outcome, intent, disc, later_aggregate=False,
                  origin_override=None, comp_class=None):
            # Prefer a per-line origin (later_therapies carries one per 3L+ line);
            # else the field-level `therapy_ids_provenance` origin.
            origin = origin_override or _prov(prov_field, 'origin')
            # Provenance is populated by the derivation pipeline for refreshed
            # rows. For a row derived before provenance existed, fall back to
            # 'inferred' for a resolved regimen rather than a misleading `null` —
            # never 'asserted', so a consumer may trust 'asserted' but must
            # verify 'inferred'.
            if not cid:
                # No resolved regimen concept_id → regimen_source is meaningless;
                # a null-id line must NOT inherit an aggregate 'asserted'/'inferred'
                # (e.g. an unresolved 3L+ line under an all-asserted later set).
                origin = None
            elif origin is None:
                origin = 'inferred'
            entry = {
                'line': n,
                'regimen': regimen,
                'regimen_concept_id': cid,
                'regimen_source': origin,
                'release_id': _prov(prov_field, 'release_id'),
                'component_ids': comp or [],
                # Therapy-class ("type") concept_ids for the line (ADR 0002),
                # derived from component_ids; parity with the flat
                # *_therapy_type_ids fields. For 3L+ lines this is the shared
                # later-line aggregate (like component_ids), flagged by
                # later_aggregate — an individual later line may show classes
                # from a sibling later line.
                'type_ids': comp_class or [],
                # ISO strings on the wire, consistent with the flat *_date fields
                # (DRF DateField); avoids raw date objects leaking to consumers
                # that json.dumps the payload themselves.
                'start_date': _iso(start),
                'end_date': _iso(end),
                'outcome': outcome,
                'intent': intent,
                'discontinuation_reason': disc,
            }
            if later_aggregate:
                entry['later_aggregate'] = True
            return entry

        lines = []
        if obj.first_line_therapy or obj.first_line_therapy_id:
            lines.append(_line(
                1, obj.first_line_therapy, obj.first_line_therapy_id,
                'first_line_therapy_id', obj.first_line_component_ids,
                obj.first_line_start_date or obj.first_line_date, obj.first_line_end_date,
                obj.first_line_outcome, obj.first_line_intent,
                obj.first_line_discontinuation_reason,
                comp_class=obj.first_line_therapy_type_ids))
        if obj.second_line_therapy or obj.second_line_therapy_id:
            lines.append(_line(
                2, obj.second_line_therapy, obj.second_line_therapy_id,
                'second_line_therapy_id', obj.second_line_component_ids,
                obj.second_line_start_date or obj.second_line_date, obj.second_line_end_date,
                obj.second_line_outcome, obj.second_line_intent,
                obj.second_line_discontinuation_reason,
                comp_class=obj.second_line_therapy_type_ids))

        # 3L+ lines: iterate the authoritative per-line `later_therapies` list,
        # which includes lines whose regimen did not resolve to a concept_id, so
        # no later line is dropped. Each entry names its own regimen and carries
        # its own line number, concept_id, and dates; component_ids/outcome
        # remain the aggregate `later_*` values (flagged later_aggregate).
        later_therapies = [lt for lt in (obj.later_therapies or []) if isinstance(lt, dict)]
        # Only the new shape carries a per-line `concept_id` key. Records derived
        # before this change have `later_therapies` entries without that key and
        # with resolved ids in the separate `later_therapy_ids` list.
        has_per_line_ids = any('concept_id' in lt for lt in later_therapies)
        if later_therapies:
            if has_per_line_ids:
                # New shape: line number and id are authoritative per entry.
                aligned_ids = [lt.get('concept_id') for lt in later_therapies]
                line_nums = [lt.get('lineNumber') for lt in later_therapies]
            else:
                # Legacy shape: no per-line id/line. Preserve the per-line
                # entries (count, names, dates); align resolved ids positionally
                # only when every later line resolved (equal counts) — otherwise
                # the subset in later_therapy_ids can't be mapped to a line.
                later_ids = obj.later_therapy_ids or []
                aligned_ids = (later_ids if len(later_ids) == len(later_therapies)
                               else [None] * len(later_therapies))
                line_nums = [None] * len(later_therapies)
            for i, lt in enumerate(later_therapies):
                line_no = line_nums[i] if line_nums[i] is not None else (3 + i)
                lines.append(_line(
                    line_no, lt.get('therapy') or obj.later_therapy,
                    aligned_ids[i], 'later_therapy_ids',
                    obj.later_component_ids, lt.get('startDate'), lt.get('endDate'),
                    obj.later_outcome, obj.later_intent,
                    obj.later_discontinuation_reason, later_aggregate=True,
                    origin_override=lt.get('origin'),
                    comp_class=obj.later_therapy_type_ids))
        else:
            # Oldest rows with no `later_therapies` list at all: emit one entry
            # per resolved id (naming each from the concept cache), else a single
            # aggregate entry from the flat `later_therapy` text.
            later_ids = obj.later_therapy_ids or []
            if later_ids:
                cache = getattr(self, '_therapy_concept_cache', {})
                for i, cid in enumerate(later_ids):
                    c = cache.get(cid)
                    regimen_name = c.concept_name if c else obj.later_therapy
                    lines.append(_line(
                        3 + i, regimen_name, cid, 'later_therapy_ids',
                        obj.later_component_ids, obj.later_start_date or obj.later_date, obj.later_end_date,
                        obj.later_outcome, obj.later_intent,
                        obj.later_discontinuation_reason, later_aggregate=True,
                        comp_class=obj.later_therapy_type_ids))
            elif obj.later_therapy:
                lines.append(_line(
                    3, obj.later_therapy, None, 'later_therapy_ids',
                    obj.later_component_ids, obj.later_start_date, obj.later_end_date,
                    obj.later_outcome, obj.later_intent,
                    obj.later_discontinuation_reason, later_aggregate=True,
                    comp_class=obj.later_therapy_type_ids))
        return lines

    def get_therapy_release_id(self, obj):
        """Patient-level aggregate therapy-vocab release for the class ids the
        patient emits (ADR 0002 / EXACT #286 Gate 1).

        Each therapy line's `release_id` (`therapy_ids_provenance`) certifies the
        vocabulary generation that line's regimen -> components -> classes were
        derived against. EXACT matches drug-class "types" by OVERLAP against the
        patient's AGGREGATE `therapy_type_ids` (the union across all lines), so a
        single release must certify that whole union — it is well-defined only
        when every class-contributing line agrees on one release.

        Rule (unanimous-or-null, fail-closed): reduce over the lines that
        contribute `type_ids`; unanimous non-null release -> that release;
        anything weaker -> null ("overlap not release-consistent", so Gate 1
        fails closed rather than trust a possibly-stale overlap). A line is
        NOT individually release-certified — and therefore forces null — when:

        - its `release_id` is null / has no provenance entry, OR
        - its regimen did not resolve (`regimen_concept_id is None`): such a
          line still contributes class ids (expanded from components), but
          provenance is keyed by the resolved regimen id, so no per-line release
          covers it. This matters for 3L+ lines especially: `get_lines_of_therapy`
          smears the shared `later_therapy_ids` release onto EVERY later entry,
          including an unresolved sibling whose class contribution that shared
          release does not actually certify — gating on the null concept_id
          keeps that case fail-closed (codex #393 review).

        Normal derivation stamps every resolved line with the release active at
        derivation time, so a fully-resolved patient converges to one value;
        null is the safe default until per-line provenance is populated.
        """
        releases = []
        covered = set()
        for line in self.get_lines_of_therapy(obj):
            tids = line.get('type_ids')
            if not tids:
                continue  # no class ids -> not part of the overlap to certify
            rel = line.get('release_id')
            # A valid release_id is a non-empty string token
            # (_latest_published_release_id). Anything else — null, empty, or a
            # non-string/unhashable JSON value from a malformed provenance row —
            # is uncertified; reject BEFORE set() so a list/dict can't raise
            # TypeError and 500 the payload (#393 codex review). Likewise a
            # class-contributing line whose regimen did not resolve.
            if not isinstance(rel, str) or not rel or line.get('regimen_concept_id') is None:
                return None  # uncertified class-contributing line -> fail-closed
            releases.append(rel)
            covered.update(tids)
        if not releases:
            return None
        distinct = set(releases)
        if len(distinct) != 1:
            return None
        # Defense-in-depth: EXACT overlaps the stored aggregate `therapy_type_ids`.
        # Every class id in it must be vouched for by a certified line above; a
        # class present in the aggregate but emitted by no certified line (only
        # reachable via a hand-built/corrupt row, never the derivation pipeline)
        # would be un-certified -> fail closed rather than trust it (#393 review).
        if not set(obj.therapy_type_ids or []).issubset(covered):
            return None
        return distinct.pop()

    def validate_sct_date(self, value):
        if value is not None and value > localdate():
            raise serializers.ValidationError("SCT date cannot be in the future.")
        return value

    def validate_dlbcl_transformation_date(self, value):
        if value is not None and value > localdate():
            raise serializers.ValidationError("DLBCL transformation date cannot be in the future.")
        return value

    def validate_post_transformation_outcome(self, value):
        if not value:
            return value
        allowed = set(PostTransformationOutcome.objects.values_list('title', flat=True))
        if value not in allowed:
            raise serializers.ValidationError(
                f"Unrecognized post_transformation_outcome value: {value!r}. "
                f"Allowed: {sorted(allowed)}"
            )
        return value

    def validate(self, data):
        # Cross-field: transformation date/outcome require the flag, on both
        # create and PATCH (fall back to the stored value for partial updates).
        transformed = data.get(
            'transformed_to_dlbcl', getattr(self.instance, 'transformed_to_dlbcl', None))
        tx_date = data.get(
            'dlbcl_transformation_date', getattr(self.instance, 'dlbcl_transformation_date', None))
        outcome = data.get(
            'post_transformation_outcome', getattr(self.instance, 'post_transformation_outcome', None))
        if not transformed and (tx_date or outcome):
            raise serializers.ValidationError(
                "dlbcl_transformation_date and post_transformation_outcome "
                "require transformed_to_dlbcl to be true."
            )
        return data

    def validate_stem_cell_transplant_history(self, value):
        if not value:
            return value
        allowed = set(StemCellTransplant.objects.values_list('title', flat=True))
        bad = [v for v in value if not isinstance(v, str) or v not in allowed]
        if bad:
            raise serializers.ValidationError(
                f"Unrecognized stem_cell_transplant_history values: {bad}. "
                f"Allowed: {sorted(allowed)}"
            )
        return value

    def validate_sct_eligibility(self, value):
        if not value:
            return value
        allowed = set(SctEligibility.objects.values_list('title', flat=True))
        bad = [v for v in value if not isinstance(v, str) or v not in allowed]
        if bad:
            raise serializers.ValidationError(
                f"Unrecognized sct_eligibility values: {bad}. "
                f"Allowed: {sorted(allowed)}"
            )
        for transplant_type in ('autologous', 'allogeneic'):
            eligible = f'eligible for {transplant_type} SCT'
            ineligible = f'ineligible for {transplant_type} SCT'
            if eligible in value and ineligible in value:
                raise serializers.ValidationError(
                    f"Cannot be both eligible and ineligible for {transplant_type} SCT."
                )
        return value

# ---------------------------------------------------------------------------
# OMOP clinical event serializers
# ---------------------------------------------------------------------------

class ConditionOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConditionOccurrence
        fields = [
            'condition_occurrence_id', 'person', 'condition_concept',
            'condition_start_date', 'condition_start_datetime',
            'condition_end_date', 'condition_end_datetime',
            'condition_type_concept', 'condition_status_concept',
            'stop_reason', 'condition_source_value', 'condition_source_concept',
            'condition_status_source_value',
            'is_erroneous', 'erroneous_reason',
        ]
        extra_kwargs = {'condition_occurrence_id': {'required': False}}


class DrugExposureSerializer(serializers.ModelSerializer):
    class Meta:
        model = DrugExposure
        fields = [
            'drug_exposure_id', 'person', 'drug_concept',
            'drug_exposure_start_date', 'drug_exposure_start_datetime',
            'drug_exposure_end_date', 'drug_exposure_end_datetime',
            'drug_type_concept', 'stop_reason', 'quantity', 'days_supply',
            'route_concept', 'lot_number',
            'drug_source_value', 'drug_source_concept',
            'route_source_value', 'dose_unit_source_value',
            'is_erroneous', 'erroneous_reason',
        ]
        extra_kwargs = {'drug_exposure_id': {'required': False}}


class MeasurementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Measurement
        fields = [
            'measurement_id', 'person', 'measurement_concept',
            'measurement_date', 'measurement_datetime',
            'measurement_type_concept', 'operator_concept',
            'value_as_number', 'value_as_string', 'value_as_concept',
            'unit_concept', 'range_low', 'range_high',
            'measurement_source_value', 'measurement_source_concept',
            'unit_source_value', 'value_source_value',
            'is_erroneous', 'erroneous_reason',
        ]
        extra_kwargs = {'measurement_id': {'required': False}}


class ObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Observation
        fields = [
            'observation_id', 'person', 'observation_concept',
            'observation_date', 'observation_datetime',
            'observation_type_concept',
            'value_as_number', 'value_as_string', 'value_as_concept',
            'qualifier_concept', 'unit_concept',
            'observation_source_value', 'observation_source_concept',
            'unit_source_value', 'qualifier_source_value', 'value_source_value',
            'is_erroneous', 'erroneous_reason',
        ]
        extra_kwargs = {'observation_id': {'required': False}}


class ProcedureOccurrenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcedureOccurrence
        fields = [
            'procedure_occurrence_id', 'person', 'procedure_concept',
            'procedure_date', 'procedure_datetime',
            'procedure_end_date', 'procedure_end_datetime',
            'procedure_type_concept', 'modifier_concept', 'quantity',
            'procedure_source_value', 'procedure_source_concept',
            'modifier_source_value',
            'is_erroneous', 'erroneous_reason',
        ]
        extra_kwargs = {'procedure_occurrence_id': {'required': False}}


class EpisodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Episode
        fields = [
            'episode_id', 'person', 'episode_concept',
            'episode_start_date', 'episode_start_datetime',
            'episode_end_date', 'episode_end_datetime',
            'episode_number', 'episode_object_concept', 'episode_type_concept',
            'episode_source_value', 'episode_source_concept',
        ]


class EpisodeEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = EpisodeEvent
        fields = ['episode_id', 'event_id', 'episode_event_field_concept']


# ---------------------------------------------------------------------------
# PatientRecord supplementary serializers
# ---------------------------------------------------------------------------

class PatientDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientDocument
        fields = [
            'id', 'person', 'doc_type', 'title',
            'file', 'file_url', 'file_name', 'verified', 'uploaded_at',
            'status', 'effective_date',
        ]


# ---------------------------------------------------------------------------
# Clinical trial enrollment (status tracker — metadata from EXACT)
# ---------------------------------------------------------------------------

class PatientTrialEnrollmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientTrialEnrollment
        fields = ['id', 'person', 'trial_id', 'nct_id', 'status']


class ProvenanceRecordSerializer(serializers.ModelSerializer):
    record_type = serializers.CharField(source='content_type.model', read_only=True)

    class Meta:
        model = ProvenanceRecord
        fields = ['id', 'source', 'source_user_id', 'target_patient_id',
                  'modification_reason', 'created_at', 'record_type', 'object_id', 'organization']


class SurveySerializer(serializers.ModelSerializer):
    class Meta:
        model = Survey
        fields = ['id', 'external_id', 'name', 'title', 'description',
                  'status', 'disease', 'pages', 'estimated_minutes', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_pages(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('pages must be a list.')
        return value


class PatientSurveyResponseSerializer(serializers.ModelSerializer):
    survey_title = serializers.CharField(source='survey.title', read_only=True)
    survey_name = serializers.CharField(source='survey.name', read_only=True)

    class Meta:
        model = PatientSurveyResponse
        fields = ['id', 'person', 'survey', 'survey_title', 'survey_name',
                  'values', 'values_dates', 'percent_complete',
                  'started_at', 'completed_at', 'consent_date', 'consent_signature',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_percent_complete(self, value):
        if not (0 <= value <= 100):
            raise serializers.ValidationError('percent_complete must be between 0 and 100.')
        return value

    def validate_values(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('values must be a dict.')
        return value

    def validate_values_dates(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('values_dates must be a dict.')
        return value

    def validate_completed_at(self, value):
        if self.instance and self.instance.completed_at is not None and value is None:
            raise serializers.ValidationError('Cannot re-open a completed survey.')
        return value

    def update(self, instance, validated_data):
        # Strip immutable identity fields — person and survey are set on create only.
        validated_data.pop('person', None)
        validated_data.pop('survey', None)
        # Merge incoming values/values_dates into existing dicts (autosave support).
        for field in ('values', 'values_dates'):
            if field in validated_data:
                current = getattr(instance, field) or {}
                validated_data[field] = {**current, **validated_data[field]}
        return super().update(instance, validated_data)


# ---------------------------------------------------------------------------
# Patient consent serializer
# ---------------------------------------------------------------------------

class PatientConsentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientConsent
        fields = ['id', 'consent_type', 'consent_granted', 'consent_date', 'consent_document']
        read_only_fields = ['id', 'consent_type', 'consent_date', 'consent_document']


# ---------------------------------------------------------------------------
# Patient message serializer (bidirectional messaging — Phase 4b)
# ---------------------------------------------------------------------------

class PatientMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()
    reply_count = serializers.SerializerMethodField()

    class Meta:
        model = PatientMessage
        fields = ['id', 'patient_user', 'parent', 'sender', 'sender_name',
                  'subject', 'message', 'sender_is_patient', 'is_read',
                  'read_at', 'confidentiality', 'reply_count', 'created_at']
        read_only_fields = ['id', 'sender', 'sender_is_patient', 'is_read',
                            'read_at', 'sender_name', 'reply_count',
                            'created_at']
        extra_kwargs = {
            'patient_user': {'required': False},  # Auto-set by perform_create for patients
        }

    def get_sender_name(self, obj):
        if obj.sender:
            return obj.sender.name or obj.sender.email or str(obj.sender)
        return "Patient" if obj.sender_is_patient else "Provider"

    def get_reply_count(self, obj):
        if hasattr(obj, '_reply_count'):
            return obj._reply_count
        return obj.replies.count()

    # Cross-thread reply validation is in PatientMessageViewSet.perform_create
    # where the resolved patient_user is known (not in self.initial_data).


class ImmunizationSerializer(serializers.ModelSerializer):
    """Read-only serializer for immunization DrugExposure rows (route_source_value='VACCINE')."""
    vaccine_name = serializers.SerializerMethodField()
    date = serializers.DateField(source='drug_exposure_start_date')

    class Meta:
        model = DrugExposure
        fields = ['drug_exposure_id', 'vaccine_name', 'date', 'lot_number']

    def get_vaccine_name(self, obj):
        if obj.drug_concept and obj.drug_concept.concept_name:
            return obj.drug_concept.concept_name
        return obj.drug_source_value or 'Unknown vaccine'


class AllergySerializer(serializers.ModelSerializer):
    """Read-only serializer for allergy Observation rows (qualifier_source_value='ALLERGY')."""
    allergen_name = serializers.SerializerMethodField()
    criticality = serializers.CharField(source='value_as_string', default='')
    clinical_status = serializers.CharField(source='value_source_value', default='')
    recorded_date = serializers.DateField(source='observation_date')

    class Meta:
        model = Observation
        fields = ['observation_id', 'allergen_name', 'criticality', 'clinical_status', 'recorded_date']

    def get_allergen_name(self, obj):
        if obj.observation_concept and obj.observation_concept.concept_name:
            return obj.observation_concept.concept_name
        return obj.observation_source_value or 'Unknown allergen'


class InterchangeAgreementSerializer(serializers.ModelSerializer):
    """Read-only serializer for documented interchange agreements (TI.5.4#01)."""
    partner_organization_name = serializers.CharField(
        source='partner_organization.name', read_only=True, default=None,
    )
    in_effect = serializers.SerializerMethodField()

    class Meta:
        model = InterchangeAgreement
        fields = [
            'id', 'partner_name', 'partner_organization', 'partner_organization_name',
            'standards_supported', 'standard_versions',
            'effective_date', 'expiry_date', 'status', 'active', 'in_effect',
            'notes', 'created_at', 'updated_at',
        ]

    def get_in_effect(self, obj):
        return obj.is_in_effect()


class FieldConceptMappingSerializer(serializers.ModelSerializer):
    reviewer = serializers.CharField(source='reviewer.username', read_only=True, default=None)
    makes_field_writable = serializers.SerializerMethodField()

    def get_makes_field_writable(self, obj) -> bool:
        """Whether this row is complete enough to make its field editable.

        A curator can otherwise approve a mapping, see it listed as approved,
        and find the field still read-only with nothing saying why.
        """
        return bool(
            obj.status == 'approved'
            and obj.concept_id
            and obj.omop_table.strip().lower() in {'measurement', 'observation'}
            and obj.source_value
        )

    class Meta:
        model = FieldConceptMapping
        fields = [
            'id', 'field_name', 'concept', 'vocabulary_id', 'concept_code',
            'unit', 'omop_table', 'status', 'reviewer',
            'reviewed_at', 'notes', 'created_at', 'updated_at',
            # What turns an approved mapping into a writable field. Without a
            # source_value derivation cannot find the row the editor writes, so
            # the mapping stays advisory however complete it otherwise looks.
            'source_value', 'value_kind', 'type_concept_id',
            'value_vocabulary', 'multiple', 'makes_field_writable',
        ]
        read_only_fields = ['id', 'reviewer', 'reviewed_at', 'created_at', 'updated_at']

    def validate_concept_code(self, value):
        if not value:
            return value
        from omop_core.services.mappings import LAB_FIELD_TO_LOINC
        vocab_id = self.initial_data.get('vocabulary_id', '')
        # Check collision with LAB_FIELD_TO_LOINC (hardcoded LOINC mappings).
        if vocab_id == 'LOINC':
            for _field, (code, _unit, _display) in LAB_FIELD_TO_LOINC.items():
                if code == value:
                    raise serializers.ValidationError(
                        f"LOINC code {value} is already mapped to field '{_field}' via LAB_FIELD_TO_LOINC."
                    )
        return value

    def validate(self, attrs):
        field_name = attrs.get('field_name', getattr(self.instance, 'field_name', None))
        # Verify field_name is a real PatientRecord field.
        if field_name:
            concrete_names = {
                f.name for f in PatientRecord._meta.get_fields()
                if getattr(f, 'concrete', False)
            }
            if field_name not in concrete_names:
                raise serializers.ValidationError({
                    'field_name': f"'{field_name}' is not a concrete PatientRecord field."
                })
        return attrs

    def create(self, validated_data):
        request = self.context.get('request')
        if validated_data.get('status') == 'approved' and request:
            validated_data['reviewer'] = request.user
            validated_data['reviewed_at'] = timezone.now()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if validated_data.get('status') == 'approved' and instance.status != 'approved' and request:
            validated_data['reviewer'] = request.user
            validated_data['reviewed_at'] = timezone.now()
        return super().update(instance, validated_data)



class FieldSynonymSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(
        source='created_by.username', read_only=True, default=None,
    )

    class Meta:
        model = FieldSynonym
        fields = ['id', 'field_name', 'synonym_text', 'source', 'created_by', 'created_at']
        read_only_fields = ['id', 'source', 'created_by', 'created_at']


class TherapyLineDrugSerializer(serializers.Serializer):
    """One drug given in a line, named by concept."""

    concept_id = serializers.IntegerField()
    source_value = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=50,
    )


class TherapyLineWriteSerializer(serializers.Serializer):
    """A line of therapy as a clinician describes it.

    Deliberately not a ModelSerializer: a line of therapy is not a row. It is a
    set of DrugExposures grouped by an Episode through EpisodeEvent, and the
    therapy fields on ``PatientRecord`` are inferred back out of that grouping.
    This is the vocabulary of the clinic -- which line, which drugs, which dates
    -- and the service turns it into the CDM shape.
    """

    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.all())
    line_number = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)
    drugs = TherapyLineDrugSerializer(many=True, required=False)
    regimen_concept_id = serializers.IntegerField(required=False, allow_null=True)
    outcome = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
    )
    source_value = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=50,
    )

    def validate_start_date(self, value):
        if value and value > date.today():
            raise serializers.ValidationError('start_date cannot be in the future.')
        return value

    def validate(self, attrs):
        start, end = attrs.get('start_date'), attrs.get('end_date')
        if start and end and end < start:
            raise serializers.ValidationError(
                {'end_date': 'end_date cannot precede start_date.'},
            )
        # A line with neither drugs nor a named regimen groups nothing, so
        # inference reads it back as an empty line: the write would appear to
        # succeed and change none of the fields the caller was trying to set.
        if not attrs.get('drugs') and not attrs.get('regimen_concept_id'):
            raise serializers.ValidationError(
                'Provide at least one drug, or a regimen_concept_id naming the '
                'regimen. A line with neither groups no drug exposures and no '
                'therapy field would follow from it.',
            )
        return attrs
