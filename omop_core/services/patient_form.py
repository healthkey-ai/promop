"""Portable patient-field form contract; no host auth or clinical formulas.

Bindings name existing fields, never translate answer codes or clinical units.
Unknown bindings are intentionally not inferred from casing/display labels.
Computed dependencies are navigation metadata, not permission to write a verdict.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from omop_core.models import PatientRecord
from omop_core.services.field_write_service import apply_field_writes, FieldWriteResult
from omop_core.services.patient_record_service import (
    refresh_patient_record,
    recompute_patient_record_fields,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor

logger = logging.getLogger(__name__)


def parse_fact_date(raw, field):
    """The clinical date carried by a dated edit, or a 400-worthy error.

    A date is never inferred: an unknown date must not become "today" and a
    future date is not a measurement that happened (clinical-event-time-policy).
    """
    from datetime import date

    if not isinstance(raw, str) or not raw:
        raise ValueError(f'{field}: a dated result requires a date.')
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f'{field}: a result date must be YYYY-MM-DD.') from exc
    if parsed > timezone.localdate():
        raise ValueError(f'{field}: a result date cannot be in the future.')
    return parsed


def _project_side_effects(person, direct, projections, authorship=None, dates=None):
    """Mirror PatientRecord answers into OMOP without risking the saved answer.

    The record write already happened and is the source of truth; a projection
    failure must not roll it back or fail the request (#5521). ``authorship``
    carries the caller's self-report typing and provenance, exactly as the
    native PATCH does, so a patient answer never takes over a clinical fact.
    ``dates`` carries the clinical date of a dated edit, so a correction lands
    on the day the result was observed rather than the day it was typed.
    """
    acknowledged = set()
    if not projections:
        return acknowledged
    from omop_core.services.omop_projection import project_single_value
    from omop_core.services.write_descriptor import PINNED_PROJECTION_FIELDS

    # The descriptor only advertises a projection whose concepts resolved, and
    # the view only supplies a self-report type it found, so nothing here can
    # fail a foreign key at commit. The guard below is for anything else:
    # project_single_value keeps its own savepoint, but a failure before it
    # gets there must not take the saved answer with it.
    for name, projection in projections.items():
        if name not in direct or not projection:
            continue
        try:
            changed = project_single_value(
                person, name, direct[name], projection, acknowledge_existing=True,
                fact_date=(dates or {}).get(name),
                **(authorship or {}),
            )
            # A pinned current-state answer stays pending on the record even
            # after its fact is written: derivation does not read that row back
            # as the answer (matches the native PATCH's `projected` set).
            if changed and name not in PINNED_PROJECTION_FIELDS:
                acknowledged.add(name)
        except Exception:
            logger.warning(
                'OMOP projection failed for %s; the PatientRecord answer is saved', name,
            )
    return acknowledged


FIELD_BINDINGS = {
    'bmi': ['bmi', 'bmiMin', 'bmiMax'],
    'weight': ['weight'], 'height': ['height'],
    'disease': ['disease'], 'gender': ['gender'],
    'date_of_birth': ['dateOfBirth'], 'patient_age': ['patientAge', 'ageMin', 'ageMax'],
    'ecog_performance_status': ['ecogPerformanceStatus'],
    'karnofsky_performance_score': ['karnofskyPerformanceScore'],
    'planned_therapies': ['plannedTherapies'],
    'meets_slim': ['meetsSLIM'], 'meets_crab': ['meetsCRAB'],
    'clonal_plasma_cells': ['clonalPlasmaCells'],
    'kappa_flc': ['kappaFLC'], 'lambda_flc': ['lambdaFLC'],
    'bone_lesions': ['boneLesions'],
    'hemoglobin_g_dl': ['hemoglobinLevel'],
    'platelet_count_thousand_per_ul': ['plateletCount'],
    'wbc_count_thousand_per_ul': ['whiteBloodCellCount'],
    'anc_thousand_per_ul': ['absoluteNeutrophileCount'],
    'serum_calcium_mg_dl': ['serumCalciumLevel'],
    'creatinine_clearance_rate': ['creatinineClearanceRate'],
    'serum_creatinine_mg_dl': ['serumCreatinineLevel'],
    'estrogen_receptor_status': ['estrogenReceptorStatus'],
    'progesterone_receptor_status': ['progesteroneReceptorStatus'],
    'her2_status': ['her2Status'], 'hr_status': ['hrStatus'],
    'tnbc_status': ['tnbcStatus'], 'ki67_proliferation_index': ['ki67ProliferationIndex'],
    'monoclonal_protein_serum': ['monoclonalProteinSerum'],
    'monoclonal_protein_urine': ['monoclonalProteinUrine'],
    'measurable_disease_imwg': ['measurableDiseaseImwg', 'measurableDiseaseImwgRequired'],
    # CB Trial Details names these two by their PatientRecord column in
    # camelCase; without a binding the shared form cannot resolve the row even
    # though migration 0160 makes the field writable.
    'no_mental_health_disorder_status': ['noMentalHealthDisorderStatus'],
    'no_substance_use_status': ['noSubstanceUseStatus'],
    # Trial Details names the six current-diagnosis questions in camelCase
    # (#5521); without a binding the shared form cannot resolve the row.
    'mgus': ['mgus'],
    'smoldering_multiple_myeloma': ['smolderingMultipleMyeloma'],
    'waldenstrom_macroglobulinemia': ['waldenstromMacroglobulinemia'],
    'igm_multiple_myeloma': ['igmMultipleMyeloma'],
    'al_amyloidosis': ['alAmyloidosis'],
    'poems_syndrome': ['poemsSyndrome'],
    'flipi_score': ['flipiScore'], 'gelf_criteria_status': ['gelfCriteriaStatus'],
    'tumor_grade': ['tumorGrade'], 'stage': ['stage'],
    'peripheral_neuropathy_grade': ['peripheralNeuropathyGrade'],
    'largest_lymph_node_size': ['largestLymphNodeSize'], 'spleen_size': ['spleenSize'],
    'mipi_risk': ['mipiRisk', 'mipiRisksRequired'],
    'mipi_c_risk': ['mipiCRisk', 'mipiCRisksRequired'],
    'ldh_u_l': ['lactateDehydrogenaseLevel'],
    'ldh_uln': ['lactateDehydrogenaseLevelUln'],
    'high_risk_mcl_criteria': ['highRiskMclCriteriaRequired', 'highRiskMclCriteriaExcluded',
                               'highRiskMclCriteriaSufficientAny'],
}

# Ported navigation from CB SUBFORM_ATTRS_MAPPING/DERIVED_SUBFORM_INPUTS.
# Unreviewed result equivalence is explicit, rather than silently replacing the
# CB rule or allowing an input change to appear to establish that verdict.
FORM_INPUTS = {
    'bmi': ['weight', 'height'],
    # Kept in step with `write_descriptor._COMPUTED_INPUTS['meets_slim']` by a test:
    # this table is still what the form serves, so it must not disagree with the
    # derivation, which reads the FLC ratio and does not infer the MRI criterion
    # from `bone_lesions`.
    'meets_slim': ['clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'kappa_lambda_ratio'],
    'meets_crab': ['serum_calcium_mg_dl', 'creatinine_clearance_ml_min',
                   'serum_creatinine_mg_dl', 'hemoglobin_g_dl', 'bone_lesions'],
    'tnbc_status': ['estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status'],
    'hr_status': ['estrogen_receptor_status', 'progesterone_receptor_status'],
    # Navigate to existing source controls; never expose a host-withheld age
    # value or invent an LDH-ULN alias. These lists do not calculate MIPI, whose
    # separate input/result compatibility gate remains in force.
    'mipi_risk': ['date_of_birth', 'ecog_performance_status', 'ldh_u_l', 'ldh_upper_limit_normal', 'wbc_count_thousand_per_ul'],
    'mipi_c_risk': ['date_of_birth', 'ecog_performance_status', 'ldh_u_l', 'ldh_upper_limit_normal',
                    'wbc_count_thousand_per_ul', 'ki67_proliferation_index'],
    # CB's subform metadata names these source facts. PROMOP has no native
    # hepatic/haematological result column, so the nodes are navigation with a
    # result gate; the renal verdict exists but uses PROMOP's own eGFR/creatinine
    # rule. Editing an input never writes the verdict.
    'renal_adequacy_status': ['date_of_birth', 'gender', 'serum_creatinine_mg_dl',
                              'creatinine_clearance_ml_min'],
    'hepatic_adequacy_status': ['ast_u_l', 'alt_u_l', 'bilirubin_total_mg_dl',
                                'ethnicity', 'gender'],
    'haematological_adequacy_status': ['anc_thousand_per_ul',
                                       'platelet_count_thousand_per_ul', 'hemoglobin_g_dl'],
    'high_risk_mcl_criteria': ['mipi_risk', 'mipi_c_risk', 'ki67_proliferation_index',
                               'cytogenetic_markers', 'largest_lymph_node_size',
                               'spleen_size', 'alc_thousand_per_ul'],
}
COMPATIBILITY_GATES = {
    'meets_slim': 'The SLiM result is not authored by this form and its full parity with Trial Details '
                  'is pending clinical review: CB includes MRI lesions; PROMOP derives marrow/FLC criteria '
                  'with different unknown/history semantics. The source facts remain editable.',
    'meets_crab': 'The CRAB result is not authored by this form and its parity with Trial Details is '
                  'pending clinical review. The source facts it is derived from remain editable.',
    'mipi_risk': 'PROMOP does not yet expose MIPI as a reviewed PatientRecord result. '
                 'Its source facts remain editable; the result is not.',
    'mipi_c_risk': 'PROMOP does not yet expose MIPI-c as a reviewed PatientRecord result. '
                   'Its source facts remain editable; the result is not.',
    'renal_adequacy_status': 'PROMOP derives its own renal verdict from eGFR and creatinine; Trial Details '
                             'uses CB\'s clearance/eGFR rule. The result is not authored here, while the '
                             'source facts remain editable.',
    'hepatic_adequacy_status': 'PROMOP has no native hepatic-adequacy result; Trial Details derives it '
                               'from these facts in CB. The result is not authored here, while the source '
                               'facts remain editable.',
    'haematological_adequacy_status': 'PROMOP has no native haematological-adequacy result; Trial Details '
                                      'derives it from these facts in CB. The result is not authored here, '
                                      'while the source facts remain editable.',
    'high_risk_mcl_criteria': 'The complete MCL result and source-field bindings require review. '
                              'Its navigable inputs remain editable; the result is not.',
}
# Reviewed patient-facing labels and units for fields whose model verbose_name
# is not the label a patient should read, or where the unit belongs beside the
# control instead of in a separate companion field. The native tabs already use
# these; the shared form must agree with them, including inside computed
# subforms (MIPI, #4559).
FORM_LABELS = {
    'ldh_u_l': ('LDH', 'U/L'),
    'ldh_upper_limit_normal': ('LDH upper limit of normal', 'U/L'),
    'wbc_count_thousand_per_ul': ('WBC count', '10³/µL'),
    'ecog_performance_status': ('ECOG performance status', None),
    'ki67_proliferation_index': ('Ki-67 proliferation index', '%'),
    'mipi_risk': ('MIPI Risk', None),
    'mipi_c_risk': ('MIPI-C Risk', None),
    'date_of_birth': ('Date of birth', None),
}

# No PatientRecord field is refused here: death date has its dated assertion
# writer (#5602), and relapse/refractory answers are stored as the native
# serializer's therapy_overrides (#5605/#5604).
DEDICATED_WRITES = {}

# Inferred by default from therapy-line outcomes, but the native serializer
# already stores an explicit answer in PatientRecord.therapy_overrides so a
# refresh does not replace it. The shared form must use that same contract
# rather than writing the derived column directly (#5605/#5604).
THERAPY_OVERRIDE_FIELDS = frozenset({'relapse_count', 'treatment_refractory_status'})


def _attach_latest_fact_dates(record, entries):
    """Name the latest recorded date per dated field for this patient, in place.

    The date is metadata about the fact that currently wins derivation, so a
    correction can target it instead of defaulting to today. Lab result dates
    come from derivation's own selection rather than a second implementation
    of its rules.
    """
    from django.db.models import Max
    from omop_core.models import Observation

    measurement_names = set()
    measurement_concepts = {}
    observation_concepts = {}
    for name, entry in entries.items():
        if not entry.get('dated'):
            continue
        projection = entry['projection']
        if projection.get('concept_id') is None:
            continue
        if projection['omop_table'] == 'measurement':
            measurement_names.add(name)
            measurement_concepts[name] = projection['concept_id']
        else:
            observation_concepts.setdefault(
                projection['concept_id'], []).append(name)
    latest = {}
    if measurement_names:
        from omop_core.services.patient_record_service import _get_laboratory_data
        lab_dates = {}
        _get_laboratory_data(record.person, dates_out=lab_dates)
        for name in measurement_names:
            if lab_dates.get(name) is not None:
                latest[name] = lab_dates[name]
    if observation_concepts:
        rows = (
            Observation.objects.filter(
                person=record.person, is_erroneous=False,
                observation_concept_id__in=observation_concepts)
            .values('observation_concept_id').annotate(latest=Max('observation_date'))
        )
        for row in rows:
            for name in observation_concepts[row['observation_concept_id']]:
                latest[name] = row['latest']
    for name, value in latest.items():
        entries[name]['latest_date'] = value.isoformat()


def patient_form_descriptors(descriptor=None, *, readable_fields=None, writable=True, record=None):
    """Enrich the existing descriptor, without granting any additional writes."""
    descriptor = build_writable_field_descriptor() if descriptor is None else descriptor
    model_fields = {field.name: field for field in PatientRecord._meta.fields}
    result = {}
    descriptor = dict(descriptor)
    descriptor['patient_name'] = {'kind': 'direct', 'writable': True, 'target': 'patient_record', 'value_kind': 'string'}
    names = set(descriptor) | set(FORM_INPUTS)
    if readable_fields is not None:
        # Synthetic blocked computed nodes may expose input navigation, never
        # model data withheld by the embedding host.
        names &= set(readable_fields) | (set(FORM_INPUTS) - set(model_fields))
    for name in sorted(names):
        entry = dict(descriptor.get(name) or {
            'kind': 'computed', 'writable': False, 'reason': 'Computed result; edit its source inputs.',
        })
        field = model_fields.get(name)
        reviewed_label, reviewed_unit = FORM_LABELS.get(name, (None, None))
        entry['label'] = reviewed_label or entry.get('label') or (
            str(field.verbose_name).replace('_', ' ').capitalize() if field
            else name.replace('_', ' ').capitalize())
        if reviewed_unit and not entry.get('unit'):
            entry['unit'] = reviewed_unit
        entry['bindings'] = FIELD_BINDINGS.get(name, [])
        kind = field.get_internal_type() if field else ''
        entry['control'] = ('boolean' if kind == 'BooleanField' else 'number'
                            if kind in ('FloatField', 'DecimalField', 'IntegerField', 'PositiveIntegerField')
                            else 'date' if kind == 'DateField' else 'text')
        if name in FORM_INPUTS:
            entry['inputs'] = list(FORM_INPUTS[name])
            entry.update(kind='computed', writable=False)
        if name == 'planned_therapies':
            entry.update(options_source='therapy_regimens', multiple=True)
        if name in COMPATIBILITY_GATES:
            entry['compatibility_reason'] = COMPATIBILITY_GATES[name]
            # The gate withholds permission to author the RESULT, not permission
            # to record the source facts it is derived from. Those inputs are
            # ordinary writable fields; a host must keep the result read-only and
            # must not present an input edit as establishing the result.
            entry['compatibility_scope'] = 'result'
        projection = entry.get('projection') or {}
        unsupported = DEDICATED_WRITES.get(name)
        # A best-effort projection is written to PatientRecord first; the fact is
        # a side effect that need not be readable back. This is the only way an
        # occurrence target can be authored here, because an occurrence cannot
        # be derived back into the current answer.
        if (projection and not projection.get('best_effort')
                and (projection.get('omop_table') not in ('measurement', 'observation')
                     or entry.get('value_kind') == 'json')):
            unsupported = 'This field requires its dedicated OMOP resource editor.'
        if entry.get('kind') == 'editable' and not projection and 'concept_id' not in entry:
            unsupported = 'This field requires its dedicated structured-data editor.'
        if record is not None and name in ('height', 'weight'):
            unit = getattr(record, f'{name}_units')
            canonical_unit = 'cm' if name == 'height' else 'kg'
            if unit and unit != canonical_unit and getattr(record, name) is not None:
                unsupported = f'This value uses {unit}; canonical-unit editing requires a reviewed conversion.'
            entry['unit'] = unit or canonical_unit
        if unsupported:
            entry.update(writable=False, reason=unsupported)
        if not writable and entry.get('writable'):
            entry.update(writable=False, read_only_for_caller=True,
                         reason='Editing is not available for this patient in the current session.')
        result[name] = entry
    if record is not None:
        _attach_latest_fact_dates(record, result)
    return result


@transaction.atomic
def apply_patient_form_patch(person, changes, *, allowed_fields=None, descriptor=None,
                             authorship=None, patient_author=False, changed_by='system'):
    """Shared form write, with explicit acknowledgements and no no-op success.

    The host must authorize the person first. Existing OMOP mapping/provenance
    and clear restrictions remain owned by apply_field_writes. Direct columns
    retain their existing PatientRecord-only behavior (e.g. planned_therapies).
    ``authorship`` is the caller's project_single_value typing (self-report and
    provenance) for best-effort side-effect projections; ``patient_author``
    says the caller is the patient or their representative. A patient author
    with no authorship means the self-report type concept is not loaded, and
    then the side effect is skipped here, by the write itself: a patient's
    answer typed as the recipe's EHR fact would match and overwrite today's
    clinical fact in place, whichever caller forgot to say who was writing.
    """
    result = FieldWriteResult()
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record = PatientRecord.objects.select_for_update().get(pk=record.pk)
    raw_descriptor = (build_writable_field_descriptor()
                      if descriptor is None else descriptor)
    # An allowed alias may be the only thing the host lists; keep its canonical
    # entry privately for projection metadata. It is NOT added to the filtered
    # descriptor or the write surface, so an excluded canonical field stays
    # unwritable.
    canonical_entries = {
        entry.get('canonical'):
            raw_descriptor.get(entry.get('canonical')) or entry
        for entry in raw_descriptor.values()
        if entry.get('canonical')
    }
    descriptor = patient_form_descriptors(
        raw_descriptor, readable_fields=allowed_fields, record=record)
    facts, direct, person_changes, profile_applied = {}, {}, set(), set()
    therapy_overrides_applied = set()
    previous_override_values = {}
    override_values = {}
    # A dated fact carries its clinical date with the value: an edit of an
    # existing result updates that date, and a new result records the day it was
    # actually measured rather than the day it was typed
    # (docs/clinical-event-time-policy.md). Shared with the direct applier so
    # both federation PATCH paths behave the same.
    from omop_core.services.field_write_service import split_dated_edits
    changes, fact_dates = split_dated_edits(changes, descriptor)
    side_effect_projections = {}
    record_profile_fields = set()
    birth_changes = {}
    project_side_effects = not (patient_author and not authorship)
    for name, value in changes.items():
        entry = descriptor.get(name, {})
        if not entry.get('writable') or entry.get('target') != 'patient_record':
            result.rejected[name] = entry.get('reason') or 'No supported patient-record write path.'
            continue
        if name in THERAPY_OVERRIDE_FIELDS or name == 'refractory_status':
            # These are inferred from the therapy lines, but an explicit answer
            # is an override the native serializer already keeps in
            # therapy_overrides. Store it there so a refresh cannot replace it,
            # and never write the derived column as if it were a fact.
            canonical = 'treatment_refractory_status' if name == 'refractory_status' else name
            if canonical == 'relapse_count':
                if value in (None, ''):
                    override_value = None
                elif isinstance(value, bool) or (
                        isinstance(value, float) and not value.is_integer()):
                    result.rejected[name] = 'Relapse count must be a whole number.'
                    continue
                else:
                    try:
                        override_value = int(value)
                    except (TypeError, ValueError):
                        result.rejected[name] = 'Relapse count must be a whole number.'
                        continue
                    if override_value < 0:
                        result.rejected[name] = 'Relapse count cannot be negative.'
                        continue
                    try:
                        record._meta.get_field(canonical).clean(override_value, record)
                    except ValidationError as exc:
                        result.rejected[name] = '; '.join(exc.messages)
                        continue
            else:
                from omop_core.services.treatment_catalog import REFRACTORY_STATUSES
                if value in (None, ''):
                    override_value = None
                elif value not in REFRACTORY_STATUSES:
                    result.rejected[name] = f'Unknown refractory status: {value!r}.'
                    continue
                else:
                    override_value = value
            previous_override_values.setdefault(
                canonical, getattr(record, canonical, None))
            override_values[canonical] = override_value
            overrides = dict(record.therapy_overrides or {})
            if override_value is None:
                overrides.pop(canonical, None)
            else:
                overrides[canonical] = override_value
            record.therapy_overrides = overrides
            setattr(record, canonical, override_value)
            pending = set(record.user_edited_fields or [])
            if override_value is None:
                # A clear must not enter the generic pending-value backfill:
                # that is how a cleared assertion is replaced by an inferred 0.
                pending.discard(canonical)
            else:
                pending.add(canonical)
            record.user_edited_fields = sorted(pending)
            therapy_overrides_applied.add(canonical)
            result.applied.append(name)
            continue
        # Existing profile writes belong to Person, not clinical OMOP facts.
        # Keep identity handling here portable; a host may also synchronize its
        # own login/display-name account within the outer transaction.
        if name == 'patient_name':
            if not isinstance(value, str) or not value.strip():
                result.rejected[name] = 'Provide a non-empty patient name.'
                continue
            parts = value.strip().split(None, 1)
            for key, text in [('given_name', parts[0]), ('family_name', parts[1] if len(parts) > 1 else '')]:
                try:
                    person._meta.get_field(key).clean(text, person)
                except ValidationError as exc:
                    raise ValueError(f'{name}: {"; ".join(exc.messages)}') from exc
                setattr(person, key, text)
                person_changes.add(key)
            result.applied.append(name)
            profile_applied.add(name)
            continue
        if name in ('date_of_birth', 'date_of_birth_precision'):
            try:
                birth_changes[name] = record._meta.get_field(name).clean(None if value == '' else value, record)
            except ValidationError as exc:
                raise ValueError(f'{name}: {"; ".join(exc.messages)}') from exc
            if name == 'date_of_birth' and birth_changes[name] and birth_changes[name] > timezone.localdate():
                raise ValueError('date_of_birth: Date of birth cannot be in the future.')
            result.applied.append(name)
            profile_applied.add(name)
            continue
        if name in ('phone_number', 'email'):
            try:
                value = record._meta.get_field(name).clean(None if value == '' else value, record)
            except ValidationError as exc:
                raise ValueError(f'{name}: {"; ".join(exc.messages)}') from exc
            value = value.strip() if isinstance(value, str) else value
            updates = {name: value}
            setattr(record, name, value)
            record_profile_fields.add(name)
            for key, item in updates.items():
                setattr(person, key, item)
                person_changes.add(key)
            result.applied.append(name)
            profile_applied.add(name)
            continue
        projection = entry.get('projection') or {}
        # PatientRecord-first: the record is the source of truth, and every
        # mapped scalar is stored there before its OMOP fact is attempted as a
        # best-effort side effect. Only dedicated clinical-input recipes (which
        # own their writer) and profile fields still take the legacy facts path.
        # Profile fields move to the direct path in the profile-projection PR.
        best_effort = (entry.get('kind') == 'direct'
                       and entry.get('target') == 'patient_record'
                       and projection.get('best_effort') is True)
        scalar_projection = (
            entry.get('kind') == 'direct'
            and entry.get('target') == 'patient_record'
            and not entry.get('clinical_input_contract')
            and entry.get('value_kind') != 'json'
            and entry.get('projection_target') not in ('person', 'location')
            and projection.get('omop_table') in ('measurement', 'observation')
        )
        if best_effort or scalar_projection:
            from omop_core.services.omop_projection import projection_for_descriptor
            side_effect_projections[name] = projection_for_descriptor(entry)
        if ((not entry.get('projection') or best_effort or scalar_projection)
                and entry.get('projection_target') not in ('person', 'location')
                and entry.get('kind') == 'direct'):
            field = record._meta.get_field(name)
            if not field.concrete or field.is_relation or field.primary_key:
                result.rejected[name] = 'Not an editable patient-record column.'
                continue
            if (scalar_projection and entry.get('value_kind') != 'number'
                    and value is not None and len(str(value)) > 60):
                # Keep the legacy scalar contract: a rejected edit is better
                # than a successful, silently truncated clinical assertion.
                raise ValueError(
                    f'{name}: this scalar write supports at most 60 characters; '
                    'no value was saved.')
            try:
                # JSON numbers arrive as floats. Django's DecimalField converts
                # a float with Decimal(float), which produces a long binary
                # expansion and fails its own decimal_places validator for
                # perfectly ordinary values such as 2.4. Go through str().
                if (field.get_internal_type() == 'DecimalField'
                        and isinstance(value, (int, float)) and not isinstance(value, bool)):
                    value = Decimal(str(value))
                direct[name] = field.clean(None if value == '' else value, record)
                if name == 'death_date' and direct[name] is not None:
                    # The native PATCH validates the same two rules. A death
                    # date is an assertion about a dated event, so an unknown
                    # date must not become today and a future date is not a
                    # fact; a date before birth is not this person's.
                    if direct[name] > timezone.localdate():
                        raise ValueError('death_date: Death date cannot be in the future.')
            except ValidationError as exc:
                raise ValueError(f'{name}: {"; ".join(exc.messages)}') from exc
        else:
            # The legacy scalar applier's string column is 60 characters. A
            # rejected edit is preferable to a successful, silently truncated
            # clinical assertion. Do not change its provenance or projector.
            if ((entry.get('projection') or entry.get('kind') == 'editable')
                    and entry.get('value_kind') != 'number'
                    and value is not None and len(str(value)) > 60):
                raise ValueError(f'{name}: this scalar write supports at most 60 characters; no value was saved.')
            facts[name] = value
    # Validate the EFFECTIVE pair, not the stored birth date alone: a PATCH
    # may submit a new DOB and a death date together (the native serializer
    # validates the submitted pair), a DOB-only edit must be checked against a
    # death date already on the record, and a precision-only edit changes the
    # normalized date (month/year placeholders are not assertions).
    from omop_core.services.birth_dates import normalize_birth_date, person_birth_values
    dob, precision = normalize_birth_date(
        person, birth_changes) if birth_changes else (record.date_of_birth,
                                                      record.date_of_birth_precision)
    effective_death = direct.get('death_date', record.death_date)
    if effective_death and dob and effective_death < dob:
        raise ValueError('death_date: Death date cannot precede date of birth.')
    if birth_changes:
        for name, value in person_birth_values(dob, precision).items():
            setattr(person, name, value)
            person_changes.add(name)
        record.person = person
        record.date_of_birth = dob
        record.date_of_birth_precision = precision
        record.patient_age = None
        record_profile_fields |= {'date_of_birth', 'date_of_birth_precision', 'patient_age'}
    if person_changes:
        person.save(update_fields=sorted(person_changes))
    if record_profile_fields:
        record.save(update_fields=sorted(record_profile_fields))
    if therapy_overrides_applied:
        # The model's save applies therapy_overrides to the inferred columns;
        # one write covers the answer, its provenance marker and the override.
        record.save(update_fields=sorted(therapy_overrides_applied)
                    + ['therapy_overrides', 'user_edited_fields'])
        if project_side_effects:
            from omop_core.services.omop_projection import projection_for_descriptor
            projections = {}
            for canonical in therapy_overrides_applied:
                entry = descriptor.get(canonical) or canonical_entries.get(canonical) or {}
                projection = projection_for_descriptor(entry)
                if projection:
                    projections[canonical] = projection
            acknowledged = _project_side_effects(
                person, override_values, projections, authorship)
            if acknowledged:
                # The fact is written; the override itself is the source of
                # truth, so the pending marker is no longer needed. Keeping it
                # lets a later generic backfill overwrite a cleared assertion.
                record.user_edited_fields = sorted(
                    set(record.user_edited_fields or []) - acknowledged)
                record.save(update_fields=['user_edited_fields'])
    if direct:
        previous_direct = {name: getattr(record, name, None) for name in direct}
        for name, value in direct.items():
            setattr(record, name, value)
        record.user_edited_fields = sorted(set(record.user_edited_fields or []) | set(direct))
        record.save(update_fields=sorted(direct) + ['user_edited_fields'])
        from omop_core.services.patient_record_revisions import write_record_revisions
        write_record_revisions(record, previous_direct, changed_by=changed_by)
        result.applied.extend(direct)
        if project_side_effects:
            acknowledged = _project_side_effects(
                person, direct, side_effect_projections, authorship,
                dates=fact_dates)
            if acknowledged:
                # The fact is written, so the record no longer has to mask
                # derivation: clear only what the projection acknowledged.
                # A newer fact from another day must not be hidden forever
                # behind a marker left by today's successful save.
                record.user_edited_fields = sorted(
                    set(record.user_edited_fields or []) - acknowledged)
                record.save(update_fields=['user_edited_fields'])
    if facts:
        fact_result = apply_field_writes(person, facts, descriptor=descriptor, dates=fact_dates)
        result.applied.extend(fact_result.applied)
        result.rejected.update(fact_result.rejected)
        result.profile.update(fact_result.profile)
        # A previously direct edit must stop masking the successfully projected
        # fact once an approved mapping exists. Remove only acknowledged fields.
        pending = set(record.user_edited_fields or []) & set(fact_result.applied)
        if pending:
            record.refresh_from_db()
            record.user_edited_fields = sorted(set(record.user_edited_fields or []) - pending)
            record.save(update_fields=['user_edited_fields'])
            refresh_patient_record(person)
    if direct or profile_applied or therapy_overrides_applied:
        # PatientRecord first: the record already holds the answer, and the
        # OMOP side effect is not read back (docs/patient-record-first-writes.md).
        # There is deliberately no refresh_patient_record() here, as there is
        # none on the native PATCH: a derivation now would rank the side
        # effect against the record's own history. Recompute the dependent
        # defining events from the record's own canonical inputs instead, so
        # the saved verdict is the one the form will read back.
        record = recompute_patient_record_fields(
            PatientRecord.objects.select_for_update().get(pk=record.pk),
            changed_fields=set(direct) | profile_applied | therapy_overrides_applied,
        )
        if therapy_overrides_applied:
            # Active formulas run after the save; reapply the explicit answers
            # so the recomputation cannot replace an authored override.
            explicit = {
                name: (record.therapy_overrides or {})[name]
                for name in therapy_overrides_applied
                if name in (record.therapy_overrides or {})
            }
            if explicit:
                for name, value in explicit.items():
                    setattr(record, name, value)
                record.save(update_fields=sorted(explicit))
    record.refresh_from_db()
    if therapy_overrides_applied:
        from omop_core.services.patient_record_revisions import write_record_revisions
        # After recomputation, so a clear is recorded as old -> inferred value,
        # not old -> the transient zero the override removal left behind.
        write_record_revisions(record, previous_override_values, changed_by=changed_by)
    return record, result
