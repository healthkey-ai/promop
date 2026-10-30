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


def _project_side_effects(person, direct, projections, authorship=None):
    """Mirror PatientRecord answers into OMOP without risking the saved answer.

    The record write already happened and is the source of truth; a projection
    failure must not roll it back or fail the request (#5521). ``authorship``
    carries the caller's self-report typing and provenance, exactly as the
    native PATCH does, so a patient answer never takes over a clinical fact.
    """
    if not projections:
        return
    from omop_core.services.omop_projection import project_single_value

    # The descriptor only advertises a projection whose concepts resolved, and
    # the view only supplies a self-report type it found, so nothing here can
    # fail a foreign key at commit. The guard below is for anything else:
    # project_single_value keeps its own savepoint, but a failure before it
    # gets there must not take the saved answer with it.
    for name, projection in projections.items():
        if name not in direct or not projection:
            continue
        try:
            project_single_value(
                person, name, direct[name], projection, acknowledge_existing=True,
                **(authorship or {}),
            )
        except Exception:
            logger.warning(
                'OMOP projection failed for %s; the PatientRecord answer is saved', name,
            )


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
DEDICATED_WRITES = {
    'death_date': 'Use the dedicated dated assertion workflow; the scalar form writer cannot safely author this field.',
    'relapse_count': 'Use the treatment editor that preserves therapy overrides.',
    'treatment_refractory_status': 'Use the treatment editor that preserves therapy overrides.',
}


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
        entry['label'] = entry.get('label') or (
            str(field.verbose_name).replace('_', ' ').capitalize() if field
            else name.replace('_', ' ').capitalize())
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
    return result


@transaction.atomic
def apply_patient_form_patch(person, changes, *, allowed_fields=None, descriptor=None,
                             authorship=None, patient_author=False):
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
    descriptor = patient_form_descriptors(descriptor, readable_fields=allowed_fields, record=record)
    facts, direct, person_changes, profile_applied = {}, {}, set(), set()
    side_effect_projections = {}
    record_profile_fields = set()
    birth_changes = {}
    project_side_effects = not (patient_author and not authorship)
    for name, value in changes.items():
        entry = descriptor.get(name, {})
        if not entry.get('writable') or entry.get('target') != 'patient_record':
            result.rejected[name] = entry.get('reason') or 'No supported patient-record write path.'
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
        # PatientRecord-first: a best-effort projection is stored on the record
        # here and only mirrored into OMOP afterwards. Facts that are derived
        # back into the record keep the legacy facts path below.
        best_effort = (entry.get('kind') == 'direct'
                       and entry.get('target') == 'patient_record'
                       and projection.get('best_effort') is True)
        if best_effort:
            from omop_core.services.omop_projection import projection_for_descriptor
            side_effect_projections[name] = projection_for_descriptor(entry)
        if ((not entry.get('projection') or best_effort)
                and entry.get('projection_target') not in ('person', 'location')
                and entry.get('kind') == 'direct'):
            field = record._meta.get_field(name)
            if not field.concrete or field.is_relation or field.primary_key:
                result.rejected[name] = 'Not an editable patient-record column.'
                continue
            try:
                # JSON numbers arrive as floats. Django's DecimalField converts
                # a float with Decimal(float), which produces a long binary
                # expansion and fails its own decimal_places validator for
                # perfectly ordinary values such as 2.4. Go through str().
                if (field.get_internal_type() == 'DecimalField'
                        and isinstance(value, (int, float)) and not isinstance(value, bool)):
                    value = Decimal(str(value))
                direct[name] = field.clean(None if value == '' else value, record)
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
    if birth_changes:
        from omop_core.services.birth_dates import normalize_birth_date, person_birth_values
        dob, precision = normalize_birth_date(person, birth_changes)
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
    if direct:
        for name, value in direct.items():
            setattr(record, name, value)
        record.user_edited_fields = sorted(set(record.user_edited_fields or []) | set(direct))
        record.save(update_fields=sorted(direct) + ['user_edited_fields'])
        result.applied.extend(direct)
        if project_side_effects:
            _project_side_effects(person, direct, side_effect_projections, authorship)
    if facts:
        fact_result = apply_field_writes(person, facts, descriptor=descriptor)
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
    if direct or profile_applied:
        # PatientRecord first: the record already holds the answer, and the
        # OMOP side effect is not read back (docs/patient-record-first-writes.md).
        # There is deliberately no refresh_patient_record() here, as there is
        # none on the native PATCH: a derivation now would rank the side
        # effect against the record's own history. Recompute the dependent
        # defining events from the record's own canonical inputs instead, so
        # the saved verdict is the one the form will read back.
        record = recompute_patient_record_fields(
            PatientRecord.objects.select_for_update().get(pk=record.pk),
            changed_fields=set(direct) | profile_applied,
        )
    record.refresh_from_db()
    return record, result
