"""Copy one patient (see instance_data) from another PRomop instance into this one.

Every row gets a new id here, because the source ids belong to another
instance. Links follow the rows:

* foreign keys to copied rows point at their new ids,
* plain integer references (visit_detail_id, episode_id, ...) do too,
* polymorphic references resolve through their field concept, whose name is
  "table.column" (EpisodeEvent.event_id, Measurement.measurement_event_id),
* concepts match on (vocabulary_id, concept_code). A concept this instance
  lacks becomes 0 where a value is required and null otherwise.

Links to system data are cleared, except organization, which is the org the
patient is copied into. PatientRecord is copied and then re-derived here, so
user edits survive while derived therapy ids follow this instance's reference data.
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dataclass_field
from typing import Any, TypedDict

from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.db.models import Max, Model, Q
from psycopg import sql

from omop_core.models import (
    CareSite,
    Concept,
    ConditionEra,
    ConditionOccurrence,
    Death,
    DoseEra,
    DrugEra,
    DrugExposure,
    FhirConnection,
    Location,
    Measurement,
    MeasurementOwnership,
    Note,
    NoteNlp,
    Observation,
    ObservationPeriod,
    Organization,
    OrgInvitation,
    PatientDocument,
    PatientGroupMembership,
    PatientRecord,
    PatientTrialEnrollment,
    Person,
    PersonalRepresentative,
    PersonLanguageSkill,
    ProcedureOccurrence,
    ProvenanceRecord,
    RecordRevision,
    Specimen,
    SupportiveTherapyCourse,
    TherapyRegimen,
    TrialSearchPreferences,
    VisitDetail,
    VisitOccurrence,
    WearableUpload,
)
from omop_core.services.instance_copy import ConceptRef, concept_refs, local_concept_ids
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.pk import next_pk_batch
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import (
    AILineOfTherapySummary,
    CancerModifier,
    Episode,
    EpisodeEvent,
    Histology,
    StemTable,
)
from patient_portal.models import BreakGlassGrant, PatientInvitation, PatientUser
from prolog_surveys.models import (
    MintedParticipant,
    SurveyAnswer,
    SurveyCaptureConsent,
    SurveyConsent,
    SurveyInvitation,
    SurveyLinkedContact,
    SurveyResponse,
    SurveyVersion,
)

Columns = dict[str, Any]
# Column to set later, its target table or field concept id, and the source id.
Deferred = tuple[str, 'type[Model] | int', Any]


class PatientPayload(TypedDict):
    """One patient read from the source, with what is needed to resolve their links."""
    person: Columns
    location: Columns | None
    rows: dict[str, list[Columns]]
    concepts: dict[int, ConceptRef]
    regimens: dict[int, str]
    survey_versions: dict[int, tuple[str, str]]
    content_types: dict[int, tuple[str, str]]
    event_tables: dict[int, str]


class PatientCopyError(Exception):
    """The patient cannot be copied as asked."""


@dataclass(frozen=True)
class PatientTable:
    """A patient table and how to find one patient's rows in it.

    lookup filters by person id, unless parent is set: then it filters by the
    ids already read from parent, for tables that link by a plain id.
    """
    model: type[Model]
    lookup: str
    parent: type[Model] | None = None


# Write order: a table comes after every table whose new ids it needs at insert.
PATIENT_TABLES: tuple[PatientTable, ...] = (
    PatientTable(PersonLanguageSkill, 'person_id'),
    PatientTable(ObservationPeriod, 'person_id'),
    PatientTable(VisitOccurrence, 'person_id'),
    PatientTable(VisitDetail, 'person_id'),
    PatientTable(ConditionOccurrence, 'person_id'),
    PatientTable(DrugExposure, 'person_id'),
    PatientTable(ProcedureOccurrence, 'person_id'),
    PatientTable(Measurement, 'person_id'),
    PatientTable(Observation, 'person_id'),
    PatientTable(Death, 'person_id'),
    PatientTable(Specimen, 'person_id'),
    PatientTable(Note, 'person_id'),
    PatientTable(NoteNlp, 'note__person_id'),
    PatientTable(ConditionEra, 'person_id'),
    PatientTable(DrugEra, 'person_id'),
    PatientTable(DoseEra, 'person_id'),
    PatientTable(Episode, 'person_id'),
    PatientTable(EpisodeEvent, 'episode_id__in', parent=Episode),
    PatientTable(CancerModifier, 'person_id'),
    PatientTable(StemTable, 'person_id'),
    PatientTable(Histology, 'person_id'),
    PatientTable(AILineOfTherapySummary, 'episode__person_id'),
    PatientTable(MeasurementOwnership, 'measurement_id__in', parent=Measurement),
    PatientTable(SupportiveTherapyCourse, 'person_id'),
    PatientTable(WearableUpload, 'person_id'),
    PatientTable(PatientDocument, 'person_id'),
    PatientTable(PatientTrialEnrollment, 'person_id'),
    PatientTable(TrialSearchPreferences, 'person_id'),
    PatientTable(PatientRecord, 'person_id'),
    PatientTable(RecordRevision, 'patient_record__person_id'),
    PatientTable(SurveyResponse, 'participant_id'),
    PatientTable(SurveyAnswer, 'response__participant_id'),
    PatientTable(SurveyConsent, 'response__participant_id'),
    PatientTable(SurveyLinkedContact, 'response__participant_id'),
    PatientTable(SurveyCaptureConsent, 'response__participant_id'),
    PatientTable(MintedParticipant, 'participant_id'),
)

# Plain integer columns that hold another row's id. None: the target is not
# copied, so the reference is cleared.
_INT_REFS: dict[str, type[Model] | None] = {
    'visit_detail_id': VisitDetail,
    'preceding_visit_occurrence_id': VisitOccurrence,
    'preceding_visit_detail_id': VisitDetail,
    'parent_visit_detail_id': VisitDetail,
    'episode_parent_id': Episode,
    'episode_id': Episode,
    'measurement_id': Measurement,
    'visit_occurrence_id': VisitOccurrence,
    'provider_id': None,
    'care_site_id': None,
}

# Polymorphic id column and the column holding its field concept.
_EVENT_REFS: dict[type[Model], tuple[str, str]] = {
    EpisodeEvent: ('event_id', 'episode_event_field_concept_id'),
    Measurement: ('measurement_event_id', 'meas_event_field_concept_id'),
    Observation: ('observation_event_id', 'obs_event_field_concept_id'),
}

# System rows that would be deleted with the person, or that point at it by id.
_SYSTEM_LINKS: tuple[tuple[type[Model], str], ...] = (
    (PatientUser, 'person_id'), (FhirConnection, 'person_id'),
    (PatientInvitation, 'person_id'), (OrgInvitation, 'person_id'),
    (SurveyInvitation, 'participant_id'), (PersonalRepresentative, 'person_id'),
    (PatientGroupMembership, 'person_id'), (BreakGlassGrant, 'person_id'),
)


@dataclass
class PatientCopyStats:
    """What a patient copy did."""
    person_id: int | None = None
    created: Counter[str] = dataclass_field(default_factory=Counter)
    skipped: Counter[str] = dataclass_field(default_factory=Counter)
    missing_concepts: Counter[str] = dataclass_field(default_factory=Counter)
    warnings: Counter[str] = dataclass_field(default_factory=Counter)


class _Rollback(Exception):
    """Unwinds the transaction after a dry run."""


def select_person_ids(
    using: str, person_ids: list[int] | None = None, organization_id: int | None = None,
) -> list[int]:
    """Source person ids matching every given filter. At least one filter is required."""
    if not person_ids and organization_id is None:
        raise PatientCopyError('Pass person ids or a filter, so a bare run cannot copy every patient.')
    persons = Person.objects.using(using)
    if person_ids:
        persons = persons.filter(person_id__in=person_ids)
    if organization_id is not None:
        persons = persons.filter(patient_record__organization_id=organization_id)
    return list(persons.order_by('person_id').values_list('person_id', flat=True))


def read_patient(using: str, person_id: int) -> PatientPayload:
    """Read one patient and what is needed to resolve their links. Read-only."""
    person = Person.objects.using(using).filter(person_id=person_id).values().first()
    if person is None:
        raise PatientCopyError(f'Person {person_id} does not exist on the source.')

    rows: dict[str, list[Columns]] = {}
    read_ids: dict[type[Model], list[Any]] = {Person: [person_id]}
    for table in PATIENT_TABLES:
        value = read_ids[table.parent] if table.parent else person_id
        found = list(table.model.objects.using(using).filter(**{table.lookup: value}).values())
        rows[table.model._meta.label] = found
        read_ids[table.model] = [r[table.model._meta.pk.attname] for r in found]
    rows[ProvenanceRecord._meta.label], content_types = _read_provenance(using, read_ids)
    field_concept_ids = {
        row[field_attname] for model, (_, field_attname) in _EVENT_REFS.items()
        for row in rows[model._meta.label] if row[field_attname] is not None
    }

    concept_attnames = {f.attname for f in _concept_fields(Person)} | {
        f.attname for t in PATIENT_TABLES for f in _concept_fields(t.model)
    }
    concept_ids = {person[a] for a in concept_attnames if a in person} | {
        row[a] for table_rows in rows.values() for row in table_rows
        for a in concept_attnames if a in row
    }
    return {
        'person': person,
        'location': (
            Location.objects.using(using).filter(location_id=person['location_id']).values().first()
            if person['location_id'] else None
        ),
        'rows': rows,
        'concepts': concept_refs(using, concept_ids),
        'regimens': dict(TherapyRegimen.objects.using(using).filter(
            pk__in={r['regimen_id'] for r in rows[SupportiveTherapyCourse._meta.label]},
        ).values_list('pk', 'code')),
        'survey_versions': {
            pk: (slug, version) for pk, slug, version in SurveyVersion.objects.using(using).filter(
                pk__in={r['survey_version_id'] for r in rows[SurveyResponse._meta.label]},
            ).values_list('pk', 'survey__slug', 'version')
        },
        'content_types': content_types,
        # The table a field concept names is a fact about the source row, so it
        # is read there: this instance may not have the CDM vocabulary.
        'event_tables': {
            concept_id: name.split('.')[0] for concept_id, name in Concept.objects.using(using)
            .filter(concept_id__in=field_concept_ids).values_list('concept_id', 'concept_name')
        },
    }


def _read_provenance(
    using: str, read_ids: dict[type[Model], list[Any]],
) -> tuple[list[Columns], dict[int, tuple[str, str]]]:
    """Provenance of any copied row, found through its generic foreign key."""
    # Content type ids differ per instance, so they are looked up on the source.
    by_label = {
        (ct.app_label, ct.model): ct.pk for ct in ContentType.objects.using(using).filter(
            app_label__in={m._meta.app_label for m in read_ids},
        )
    }
    match = Q(pk__in=[])
    for model, ids in read_ids.items():
        content_type_id = by_label.get((model._meta.app_label, model._meta.model_name))
        if ids and content_type_id and not isinstance(ids[0], uuid.UUID):
            match |= Q(content_type_id=content_type_id, object_id__in=ids)
    found = list(ProvenanceRecord.objects.using(using).filter(match).values())
    return found, {pk: label for label, pk in by_label.items()}


def _concept_fields(model: type[Model]) -> list[models.Field]:
    """Foreign keys of model that point at Concept."""
    return [f for f in model._meta.concrete_fields if f.many_to_one and f.related_model is Concept]


def apply_patient(
    payload: PatientPayload, organization: Organization, target_person_id: int | None = None,
    replace: bool = False, dry_run: bool = False,
) -> PatientCopyStats:
    """Write a read_patient payload as a patient of organization, in one transaction."""
    from patient_portal.webhooks import suppress_webhook_events

    stats = PatientCopyStats()
    person_id = target_person_id or payload['person']['person_id']
    try:
        with transaction.atomic(), suppress_patient_record_refresh():
            # A copy writes every table this patient has, so the per-row signals
            # would fire once per row — and on --replace, one `deleted` per row
            # of the data being replaced, which is not what happened to the
            # patient. Subscribers get one aggregate per table instead, the same
            # shape the bulk API and FHIR-sync writers publish. Inside the
            # transaction, so the outbox rows commit with the data and a dry run
            # takes them back.
            previous_org_id = None
            with suppress_webhook_events():
                if Person.objects.filter(person_id=person_id).exists():
                    if not replace:
                        raise PatientCopyError(
                            f'Person {person_id} already exists here. Use --replace or --target-person-id.'
                        )
                    # Read before the delete: --replace may move the patient to
                    # another organization, and the one losing them has to hear
                    # about it while its record still says so.
                    previous_org_id = (PatientRecord.objects.filter(person_id=person_id)
                                       .values_list('organization_id', flat=True).first())
                    delete_patient(person_id)
                _Copier(payload, organization, person_id, stats).run()
                refresh_patient_record(Person.objects.get(person_id=person_id))
                # The source record may have no org, or there may be no source record at all.
                PatientRecord.objects.filter(person_id=person_id).update(organization=organization)
            stats.person_id = person_id
            _publish_copy_events(person_id, stats, previous_org_id, organization.pk)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return stats


def _publish_copy_events(person_id: int, stats: PatientCopyStats,
                         previous_org_id: int | None, organization_id: int) -> None:
    """One aggregate per table for a patient this instance just wrote.

    Driven by the webhook model list rather than by everything the copy
    touched: a subscriber's vocabulary is those tables, and announcing a
    table it never hears about from any other writer would be a new event
    shape rather than the same news by a different route.

    A --replace into a different organization moves the patient. The events
    below go to the organization that now holds them, and would leave the one
    that lost them believing it still has data that has been deleted, so that
    organization is told first — while `person_id` is still its own reference
    for the patient.
    """
    from django.apps import apps
    from patient_portal.webhooks import (
        PATIENT_EVENT_MODELS, publish_event, publish_patient_bulk_change,
    )

    if previous_org_id is not None and previous_org_id != organization_id:
        publish_event(previous_org_id, 'patient.changed', {
            'person_id': person_id, 'resource_type': 'omop_core.person',
            'operation': 'bulk_deleted', 'count': 1,
        })
    for label in PATIENT_EVENT_MODELS:
        model = apps.get_model(label)
        count = stats.created.get(model._meta.object_name, 0)
        publish_patient_bulk_change(person_id, model._meta.model_name, count,
                                    app_label=model._meta.app_label)


class _Copier:
    """One patient copy: new ids, remapped links, references resolved at the end."""

    def __init__(self, payload: PatientPayload, organization: Organization, person_id: int,
                 stats: PatientCopyStats) -> None:
        self.payload = payload
        self.organization = organization
        self.person_id = person_id
        self.stats = stats
        self.ids: dict[type[Model], dict[Any, Any]] = defaultdict(dict)
        self.deferred: list[tuple[Model, Deferred]] = []
        found = local_concept_ids(payload['concepts'].values())
        self.concepts: dict[int, int | None] = {
            src: found.get(ref) for src, ref in payload['concepts'].items()
        }
        here = dict(TherapyRegimen.objects.filter(
            code__in=set(payload['regimens'].values())).values_list('code', 'pk'))
        self.regimens: dict[int, int] = {
            src: here[code] for src, code in payload['regimens'].items() if code in here
        }
        self.survey_versions: dict[int, int] = {}
        for src, (slug, version) in payload['survey_versions'].items():
            pk = SurveyVersion.objects.filter(survey__slug=slug, version=version).values_list('pk', flat=True).first()
            if pk is not None:
                self.survey_versions[src] = pk
        self.by_table: dict[str, type[Model]] = {t.model._meta.db_table: t.model for t in PATIENT_TABLES}

    def run(self) -> None:
        """Copy the person, then every table in order, then the late references."""
        self._copy_person()
        for table in (*PATIENT_TABLES, PatientTable(ProvenanceRecord, '')):
            self._copy_table(table.model, self.payload['rows'][table.model._meta.label])
        self._resolve_deferred()

    def _copy_person(self) -> None:
        """Copy the person and their address under new ids."""
        location = self.payload['location']
        location_id = None
        if location is not None:
            location_id = _new_ids(Location, 'location_id', 1)[0]
            Location.objects.create(**{**location, 'location_id': location_id})
            self.stats.created['Location'] += 1
        values = self._with_concepts(Person, self.payload['person'])
        # actor_iss and actor_sub link a login on the source instance.
        values.update(person_id=self.person_id, location_id=location_id, provider_id=None,
                      care_site_id=None, actor_iss=None, actor_sub=None)
        Person.objects.create(**values)
        self.ids[Person][self.payload['person']['person_id']] = self.person_id
        self.stats.created['Person'] += 1

    def _copy_table(self, model: type[Model], rows: list[Columns]) -> None:
        """Insert rows under new ids and remember old id to new id."""
        prepared = [(row, *built) for row in rows if (built := self._build(model, row)) is not None]
        if not prepared:
            return
        pk = model._meta.pk
        if isinstance(pk, models.UUIDField):
            for _, values, _ in prepared:
                values[pk.attname] = uuid.uuid4()
        elif isinstance(pk, (models.AutoField, models.BigAutoField)):
            for _, values, _ in prepared:
                del values[pk.attname]
        elif not isinstance(pk, models.OneToOneField):
            for (_, values, _), new_id in zip(prepared, _new_ids(model, pk.attname, len(prepared))):
                values[pk.attname] = new_id
        created = model.objects.bulk_create([model(**values) for _, values, _ in prepared])
        _restore_timestamps(model, created, [values for _, values, _ in prepared])
        for (row, _, deferred), obj in zip(prepared, created):
            self.ids[model][row[pk.attname]] = obj.pk
            self.deferred.extend((obj, d) for d in deferred)
        self.stats.created[model._meta.object_name] += len(created)

    def _build(self, model: type[Model], row: Columns) -> tuple[Columns, list[Deferred]] | None:
        """New column values and references to resolve later, or None to skip the row."""
        label = model._meta.object_name
        if model is PatientDocument and row['file']:
            self.stats.skipped[label] += 1
            self.stats.warnings['PatientDocument: the stored file is not copied'] += 1
            return None
        values = self._with_concepts(model, row)
        references: list[Deferred] = []
        for f in model._meta.concrete_fields:
            if row[f.attname] is None or (f.many_to_one and f.related_model is Concept):
                continue
            if f.many_to_one or f.one_to_one:
                values[f.attname] = self._link(f, row[f.attname])
                if values[f.attname] is None and not f.null:
                    self.stats.skipped[label] += 1
                    return None
            elif f.attname in _INT_REFS and not f.primary_key:
                target = _INT_REFS[f.attname]
                values[f.attname] = None
                if target is not None:
                    references.append((f.attname, target, row[f.attname]))
        if model in _EVENT_REFS and row[_EVENT_REFS[model][0]] is not None:
            attname, field_attname = _EVENT_REFS[model]
            values[attname] = None
            references.append((attname, row[field_attname], row[attname]))
        if model is RecordRevision and values['changed_by'] != 'system':
            values['changed_by'] = None
        if model is ProvenanceRecord and not self._provenance(row, values):
            self.stats.skipped[label] += 1
            return None

        deferred = []
        for reference in references:
            resolved = self._resolve(reference)
            if resolved is not None:
                values[reference[0]] = resolved
            elif model._meta.get_field(reference[0]).null:
                deferred.append(reference)
            else:
                self.stats.skipped[label] += 1
                return None
        return values, deferred

    def _with_concepts(self, model: type[Model], row: Columns) -> Columns:
        """Row with concept ids swapped for the ids here."""
        values = dict(row)
        for f in _concept_fields(model):
            if row[f.attname] is None:
                continue
            concept_id = self.concepts.get(row[f.attname])
            if concept_id is None:
                vocabulary_id, code = self.payload['concepts'].get(row[f.attname], ('?', row[f.attname]))
                self.stats.missing_concepts[f'{vocabulary_id}:{code}'] += 1
                concept_id = None if f.null else 0
            values[f.attname] = concept_id
        return values

    def _link(self, f: models.Field, source_value: Any) -> Any:
        """New value of a foreign key. None when the target is not copied."""
        related = f.related_model
        if related is Person:
            return self.person_id
        if related is Organization:
            return self.organization.pk
        if related is ContentType:
            return source_value
        if related is TherapyRegimen:
            resolved = self.regimens.get(source_value)
        elif related is SurveyVersion:
            resolved = self.survey_versions.get(source_value)
        elif related in self.by_table.values():
            resolved = self.ids[related].get(source_value)
        else:
            return None
        if resolved is None:
            self.stats.warnings[f'{f.model._meta.object_name}.{f.name}: {related._meta.object_name} is not here'] += 1
        return resolved

    def _provenance(self, row: Columns, values: Columns) -> bool:
        """Point provenance at the copied row. False when that row was not copied."""
        app_label, model_name = self.payload['content_types'][row['content_type_id']]
        content_type = ContentType.objects.get_by_natural_key(app_label, model_name)
        object_id = self.ids[content_type.model_class()].get(row['object_id'])
        if object_id is None:
            return False
        values.update(content_type_id=content_type.pk, object_id=object_id)
        if row['target_patient_id'] == str(self.payload['person']['person_id']):
            values['target_patient_id'] = str(self.person_id)
        return True

    def _resolve(self, reference: Deferred) -> Any:
        """New id a reference points at, or None when not copied yet."""
        _, target, source_value = reference
        if isinstance(target, int):
            target = self._event_table(target)
        return self.ids[target].get(source_value) if target else None

    def _event_table(self, field_concept_id: int) -> type[Model] | None:
        """The table an event field concept names, e.g. 'drug_exposure.drug_exposure_id'."""
        return self.by_table.get(self.payload['event_tables'].get(field_concept_id, ''))

    def _resolve_deferred(self) -> None:
        """Set references to rows that were written after the row pointing at them."""
        updates: dict[tuple[type[Model], str], list[Model]] = defaultdict(list)
        for obj, reference in self.deferred:
            resolved = self._resolve(reference)
            if resolved is None:
                self.stats.warnings[f'{type(obj).__name__}.{reference[0]}: the referenced row was not copied'] += 1
                continue
            setattr(obj, reference[0], resolved)
            updates[(type(obj), reference[0])].append(obj)
        for (model, attname), objs in updates.items():
            model.objects.bulk_update(objs, [attname])


def _restore_timestamps(model: type[Model], created: list[Model], values: list[Columns]) -> None:
    """Put back source timestamps that auto_now and auto_now_add replaced on insert."""
    stamped = [f.attname for f in model._meta.concrete_fields if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False)]
    if not stamped:
        return
    for obj, source in zip(created, values):
        for attname in stamped:
            setattr(obj, attname, source[attname])
    # bulk_update does not run the auto_now hooks.
    model.objects.bulk_update(created, stamped)


def _new_ids(model: type[Model], attname: str, count: int) -> list[int]:
    """New integer ids, from the table's sequence when it has one.

    Only some OMOP tables got a sequence (observation_period, histology and
    cancer_modifier have none on staging), so the rest count up from MAX.
    """
    table = model._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute('SELECT to_regclass(%s)', [f'{table}_{attname}_seq'])
        if cursor.fetchone()[0] is not None:
            return next_pk_batch(model, attname, count)
        # Held until commit, so a concurrent insert cannot take the same ids.
        cursor.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(sql.Identifier(table)))
    start = (model.objects.aggregate(top=Max(attname))['top'] or 0) + 1
    return list(range(start, start + count))


def delete_patient(person_id: int) -> None:
    """Delete one patient's data here. Refuses when system data points at them."""
    blockers = sorted(
        model._meta.object_name for model, lookup in _SYSTEM_LINKS
        if model.objects.filter(**{lookup: person_id}).exists()
    )
    if blockers:
        raise PatientCopyError(
            f'Person {person_id} is linked to {", ".join(blockers)}. Remove those links first.'
        )
    ids: dict[type[Model], list[Any]] = {Person: [person_id]}
    for table in PATIENT_TABLES:
        value = ids[table.parent] if table.parent else person_id
        ids[table.model] = list(table.model.objects.filter(**{table.lookup: value}).values_list('pk', flat=True))
    for model, pks in ids.items():
        if pks and not isinstance(pks[0], uuid.UUID):
            ProvenanceRecord.objects.filter(
                content_type=ContentType.objects.get_for_model(model), object_id__in=pks,
            ).delete()
    for model in (EpisodeEvent, MeasurementOwnership, SurveyResponse):
        model.objects.filter(pk__in=ids[model]).delete()
    person = Person.objects.get(person_id=person_id)
    location_id = person.location_id
    person.delete()
    in_use = Person.objects.filter(location_id=location_id).exists() or CareSite.objects.filter(location_id=location_id).exists()
    if location_id and not in_use:
        Location.objects.filter(location_id=location_id).delete()
