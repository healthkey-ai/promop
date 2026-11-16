"""Disease profiles: which PatientRecord fields belong to which disease (#2331).

A patient's record is a **profile**: the base record every patient has, plus
the **extension** of each disease the patient has. A field's **scope** is
``base`` (no ``FieldDiseaseMembership`` rows) or ``disease`` (one row per
disease whose extension it is in). A profile is a logical schema: the
PatientRecord table and ``custom_fields`` are untouched by anything here.

The diseases are Disease Category management's (#2120): the enumerated set is
``DiseaseCategoryChoice``, and which category a record's disease name belongs
to is ``DiseaseCategoryMapping`` through ``genomics_catalog.disease_code``.
There is no second disease list. ``Other`` has no extension: its profile is
the base record.

This module is read-only as far as patients are concerned. Nothing here
decides what a tab renders or what a save accepts; those are #2332 and #2333.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from django.db import transaction

SCOPE_BASE = 'base'
SCOPE_DISEASE = 'disease'
# The category every unrecognised disease falls into. It has no extension.
OTHER = 'Other'
SEED_PROVENANCE = 'disease-tab-seed v1'

# The fields DiseaseTab.tsx renders for each disease, section by section: the
# extensions MM, BC, FL, MCL and CLL start with. tests/test_disease_profiles.py
# fails when this and the tab drift apart. It is a seed, not a reader: once
# seeded, the rows are what every reader uses and a curator may change them.
SEED_DISEASE_FIELDS: dict[str, frozenset[str]] = {
    'BC': frozenset({
        'histologic_type', 'tumor_grade', 'menopausal_status', 'tumor_stage', 'nodes_stage',
        'staging_modalities', 'distant_metastasis_stage', 'bone_only_metastasis_status',
        'measurable_disease_by_recist_status', 'estrogen_receptor_status',
        'progesterone_receptor_status', 'her2_status', 'hr_status', 'hrd_status',
        'androgen_receptor_status', 'ki67_proliferation_index', 'pd_l1_tumor_cells',
        'oncotype_dx_score', 'test_methodology', 'test_date', 'test_specimen_type',
        'report_interpretation', 'lymph_node_status', 'metastasis_status',
        'pd_l1_combined_positive_score', 'pd_l1_ic_percentage', 'pd_l1_assay',
    }),
    'FL': frozenset({
        'stage', 'tumor_grade', 'bulky_disease',
        'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
        'transformed_to_dlbcl',
        'dlbcl_transformation_date', 'post_transformation_outcome', 'bone_marrow_involvement',
        'clonal_bone_marrow_b_lymphocytes', 'ldh_upper_limit_normal', 'number_of_nodal_sites',
    }),
    'MM': frozenset({
        'myeloma_type', 'stage', 'progression', 'measurable_disease_imwg', 'mrd_status',
        'meets_crab', 'meets_slim', 'stem_cell_transplant_history', 'sct_date', 'sct_eligibility',
        'monoclonal_protein_serum', 'monoclonal_protein_urine', 'kappa_flc', 'lambda_flc',
        'kappa_lambda_ratio', 'involved_uninvolved_ratio', 'bone_lesions', 'clonal_plasma_cells',
        'cytogenetic_markers',
    }),
    'CLL': frozenset({
        'binet_stage', 'tumor_burden', 'disease_activity', 'richter_transformation',
        'protein_expressions', 'absolute_lymphocyte_count', 'lymphocyte_doubling_time',
        'serum_beta2_microglobulin_level', 'clonal_b_lymphocyte_count',
        'clonal_bone_marrow_b_lymphocytes', 'qtcf_value', 'largest_lymph_node_size', 'spleen_size',
        'bone_marrow_involvement', 'measurable_disease_iwcll', 'splenomegaly', 'hepatomegaly',
        'lymphadenopathy', 'autoimmune_cytopenias_refractory_to_steroids',
        'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory',
    }),
    'MCL': frozenset({
        'stage', 'protein_expressions', 'ki67_proliferation_index', 'lymphadenopathy',
        'splenomegaly', 'spleen_size', 'hepatomegaly', 'largest_lymph_node_size',
    }),
}


class ScopeError(ValueError):
    """A scope a field cannot be given: an unknown category, or ``Other``."""


def category_codes() -> set[str]:
    """Every Disease Category code, from the managed table.

    The enum is the fallback for a database whose table is empty (a test
    database built without migrations), as it is for the category endpoints.
    """
    from omop_core.models import DiseaseCategory, DiseaseCategoryChoice

    codes = set(DiseaseCategoryChoice.objects.values_list('code', flat=True))
    return codes or {choice.value for choice in DiseaseCategory}


def extension_codes() -> tuple[str, ...]:
    """The categories that can have an extension: every one but ``Other``."""
    return tuple(sorted(category_codes() - {OTHER}))


def memberships() -> dict[str, list[str]]:
    """``{field_name: [disease, ...]}`` for every disease-specific field, one query."""
    from omop_core.models import FieldDiseaseMembership

    found: dict[str, list[str]] = defaultdict(list)
    for field_name, disease in (FieldDiseaseMembership.objects
                                .order_by('field_name', 'disease')
                                .values_list('field_name', 'disease')):
        found[field_name].append(disease)
    return dict(found)


def extension_fields(code: str) -> set[str]:
    """The fields in one disease's extension."""
    from omop_core.models import FieldDiseaseMembership

    return set(FieldDiseaseMembership.objects.filter(disease=code)
               .values_list('field_name', flat=True))


def extension_sizes() -> dict[str, int]:
    """How many fields each disease's extension holds; a category with none is absent."""
    from django.db.models import Count
    from omop_core.models import FieldDiseaseMembership

    return dict(FieldDiseaseMembership.objects.values_list('disease')
                .annotate(n=Count('id')).values_list('disease', 'n'))


def scope_of(diseases: Iterable[str]) -> str:
    return SCOPE_DISEASE if diseases else SCOPE_BASE


def in_profile(field_diseases: Iterable[str], profile: Iterable[str]) -> bool:
    """Whether a field is part of the record of a patient with ``profile``'s diseases.

    A base field is in every profile. A disease-specific field is in the
    profile of any patient who has one of its diseases, so a patient with two
    cancers gets the union of their extensions.
    """
    field_diseases = set(field_diseases)
    return not field_diseases or bool(field_diseases & set(profile))


def clean_codes(codes: Iterable[str]) -> list[str]:
    """The diseases a scope may name, de-duplicated and sorted.

    Raises ``ScopeError`` for a code that is not a Disease Category, and for
    ``Other``: a field every other patient has is a base field, not a member
    of an "Other" extension.
    """
    wanted = sorted({str(code).strip() for code in codes if str(code).strip()})
    known = category_codes()
    unknown = [code for code in wanted if code not in known]
    if unknown:
        raise ScopeError(
            f'Unknown disease {", ".join(unknown)}. Choose from the Disease Categories: '
            f'{", ".join(sorted(known - {OTHER}))}.'
        )
    if OTHER in wanted:
        raise ScopeError(f'"{OTHER}" has no extension: its record is the base record. '
                         'Leave the scope as base instead.')
    return wanted


def set_field_diseases(field_name: str, codes: Iterable[str], *, user=None,
                       provenance: str = '') -> list[str]:
    """Make ``codes`` the diseases ``field_name`` belongs to; none makes it base.

    A membership edit and nothing else: no mapping, placement or stored value
    changes, a created field's key in ``custom_fields`` included. Rows the new
    scope keeps are left as they are, so their provenance still names whoever
    added them.
    """
    from omop_core.models import FieldDiseaseMembership
    from omop_core.services.field_mapping_provenance import CURATOR_PROVENANCE, user_provenance

    wanted = clean_codes(codes)
    label = provenance or (user_provenance(user) if user is not None else CURATOR_PROVENANCE)
    with transaction.atomic():
        rows = FieldDiseaseMembership.objects.filter(field_name=field_name)
        rows.exclude(disease__in=wanted).delete()
        held = set(rows.values_list('disease', flat=True))
        FieldDiseaseMembership.objects.bulk_create([
            FieldDiseaseMembership(field_name=field_name, disease=code, provenance=label,
                                   created_by=user if getattr(user, 'pk', None) else None)
            for code in wanted if code not in held
        ])
    return wanted


def seed(membership_model=None, category_model=None) -> int:
    """Write the known extensions; additive, so a replay changes nothing.

    A row a curator removed is not a row that exists, so a replay would put it
    back: the seed runs once, from its migration. A category this instance's
    curators deleted is skipped rather than given members. The models are
    parameters so the migration can pass its historical ones.
    """
    from omop_core import models

    membership_model = membership_model or models.FieldDiseaseMembership
    category_model = category_model or models.DiseaseCategoryChoice
    known = set(category_model.objects.values_list('code', flat=True))
    held = set(membership_model.objects.values_list('field_name', 'disease'))
    rows = [
        membership_model(field_name=field_name, disease=code, provenance=SEED_PROVENANCE)
        for code, fields in sorted(SEED_DISEASE_FIELDS.items())
        if not known or code in known
        for field_name in sorted(fields)
        if (field_name, code) not in held
    ]
    membership_model.objects.bulk_create(rows)
    return len(rows)


def profile_codes(values: Iterable[str]) -> set[str]:
    """The diseases a ``?disease=`` filter names, as extension codes.

    Accepts a Disease Category code in any case, or a disease name
    ``disease_code`` resolves. ``Other`` is accepted and names no extension.
    Raises ``ScopeError`` for anything else.
    """
    from omop_core.services.genomics_catalog import disease_code

    known = {code.lower(): code for code in category_codes()}
    codes = set()
    for value in values:
        text = str(value or '').strip()
        code = known.get(text.lower()) or disease_code(text)
        if code not in known.values():
            raise ScopeError(f'Unknown disease {text!r}. Choose from the Disease Categories: '
                             f'{", ".join(sorted(known.values()))}.')
        codes.add(code)
    return codes - {OTHER}


def annotate(descriptor: dict[str, dict]) -> dict[str, dict]:
    """A ``{field: entry}`` descriptor with ``scope`` and ``diseases`` on each entry.

    An alias mirrors its canonical field, so it shares that field's scope.
    The entries are copied: the builder's own dicts are left as they were.
    """
    scoped = memberships()
    annotated = {}
    for field_name, entry in descriptor.items():
        diseases = scoped.get(field_name) or scoped.get(entry.get('canonical') or '', [])
        annotated[field_name] = {**entry, 'scope': scope_of(diseases), 'diseases': list(diseases)}
    return annotated


def profile(descriptor: dict[str, dict], codes: Iterable[str]) -> dict[str, dict]:
    """The entries of an annotated descriptor that are part of ``codes``' profile."""
    codes = set(codes)
    return {field_name: entry for field_name, entry in descriptor.items()
            if in_profile(entry['diseases'], codes)}
