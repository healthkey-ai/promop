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

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable

from django.db import connection, transaction

# A configured logger: the module's own would drop INFO under gunicorn.
logger = logging.getLogger('omop.ops')

SCOPE_BASE = 'base'
SCOPE_DISEASE = 'disease'
# The category every unrecognised disease falls into. It has no extension.
OTHER = 'Other'
SEED_PROVENANCE = 'disease-tab-seed v1'
# The fields that say which disease a record has decide its profile, so they
# cannot themselves be in an extension.
_PROFILE_KEYS = frozenset({'disease', 'disease_slug', 'disease_slugs'})

# The fields DiseaseTab.tsx renders for each disease, section by section: the
# extensions MM, BC, FL, MCL and CLL start with. Two tests hold it in place:
# tests/test_disease_relevance.py fails when it and the tab drift apart, and
# tests/test_disease_profiles.py fails when it and the rows the migrations
# write drift apart, so a field added here needs a data migration too. It is
# a seed, not a reader: once seeded, the rows are what every reader uses and
# a curator may change them.
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


def category_codes(using: str = 'default') -> set[str]:
    """Every Disease Category code, from the managed table.

    The enum is the fallback for a database whose table is empty (a test
    database built without migrations), as it is for the category endpoints.
    """
    from omop_core.models import DiseaseCategory, DiseaseCategoryChoice

    codes = set(DiseaseCategoryChoice.objects.using(using).values_list('code', flat=True))
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


def field_names(using: str = 'default') -> set[str]:
    """Every field that can have a scope: what the field descriptors list.

    A clinical column, a genomics component or a created field. Not an
    internal or unit-companion column, which no descriptor shows, and not the
    fields that name the record's disease.
    """
    from omop_core.models import CustomPatientField, PatientRecord
    from omop_core.services.field_descriptor import _classify_field
    from omop_core.services.genomics_components import components

    names = {
        field.name for field in PatientRecord._meta.get_fields()
        if getattr(field, 'concrete', False) and field.name not in _PROFILE_KEYS
        and _classify_field(field.name) not in ('internal', 'unit')
    }
    names |= {'genetic_mutations.' + attribute['key'] for attribute in components()}
    return names | set(CustomPatientField.objects.using(using).values_list('field_name', flat=True))


def resolver() -> Callable[[object], str | None]:
    """A function from a code or a disease name to its Disease Category code.

    The categories are read once and each answer is remembered, so resolving
    a batch costs no query for a code and one lookup per distinct name. In
    order: a category code exactly as stored; a category code in another
    case; a name ``disease_code`` resolves; a name ``DiseaseCategoryMapping``
    maps to ``Other``. Anything else is ``None``.
    """
    from omop_core.models import DiseaseCategoryMapping
    from omop_core.services.genomics_catalog import disease_code

    known = category_codes()
    # Sorted, so two codes that differ only in case always fold the same way.
    by_lower = {code.lower(): code for code in sorted(known, reverse=True)}
    answers: dict[str, str | None] = {}

    def resolve(value) -> str | None:
        text = str(value or '').strip()
        if not text:
            return None
        if text in known:
            return text
        if text not in answers:
            code = by_lower.get(text.lower()) or disease_code(text)
            if code is None:
                # disease_code leaves a name mapped to Other unresolved; its
                # profile is the base record, which is an answer, not an error.
                code = (DiseaseCategoryMapping.objects.filter(disease_name__iexact=text)
                        .values_list('category', flat=True).first())
            answers[text] = code if code in known else None
        return answers[text]

    return resolve


def record_category(disease: object) -> str:
    """The Disease Category whose extension a record with this disease text has.

    ``''`` is the base record alone: no disease, a disease the mapping files
    under ``Other``, or one nothing recognises. The order is the patient
    editor's (``recordDiseaseCategory`` in ``fieldScope.ts``), so the page and
    the attestation agree on which disease section is on screen: what Disease
    Category management maps the name to, case-insensitively, decides; only a
    name the mapping does not hold falls back to the keyword rules the editor
    used before categories picked the tab (``patient_visible_fields.disease_type``).
    """
    from omop_core.models import DiseaseCategoryMapping
    from omop_core.services import patient_visible_fields as visible

    text = str(disease or '').strip()
    if not text:
        return ''
    mapped = (DiseaseCategoryMapping.objects.filter(disease_name__iexact=text)
              .values_list('category', flat=True).first())
    if mapped:
        return '' if mapped == OTHER else mapped
    return visible.DISEASE_TYPE_CATEGORY.get(visible.disease_type(text), '')


def record_categories(disease: object, slugs: Iterable[str] | None) -> list[str]:
    """Every Disease Category whose extension a record has, its primary one first.

    A record with several cancers (``PatientRecord.disease_slugs``, #2182) has
    the extension of each, not only of the cancer ``disease`` names: a patient
    with myeloma and prostate cancer has both. The primary comes from
    ``record_category``. Every other cancer is a slug, which is looked up
    among the names Disease Category management maps (each name made into its
    slug the way the record does), and only a slug it does not hold falls back
    to the keyword rules. ``Other`` and an unknown category have no extension
    and are left out.
    """
    from omop_core.models import DiseaseCategoryMapping
    from omop_core.services import patient_visible_fields as visible
    from omop_core.services.disease_episodes import disease_slug

    known = category_codes() - {OTHER}
    found: list[str] = []
    primary = record_category(disease)
    if primary in known:
        found.append(primary)
    others = [slug for slug in (slugs or []) if slug]
    if not others:
        return found
    by_slug: dict[str, str] = {}
    for name, category in DiseaseCategoryMapping.objects.values_list('disease_name', 'category'):
        slug = disease_slug(name)
        if slug:
            by_slug.setdefault(slug, category)
    for slug in others:
        category = by_slug.get(slug)
        if category is None:
            category = visible.DISEASE_TYPE_CATEGORY.get(visible.disease_type(slug.replace('-', ' ')), '')
        if category in known and category not in found:
            found.append(category)
    return found


def clean_codes(codes: Iterable[str]) -> list[str]:
    """The diseases a scope may name, de-duplicated and sorted.

    Raises ``ScopeError`` for a code that is not a Disease Category, and for
    ``Other``: a field every other patient has is a base field, not a member
    of an "Other" extension.
    """
    codes = [str(code).strip() for code in codes]
    if '' in codes:
        raise ScopeError('A disease code cannot be blank. Send no diseases for the base record.')
    wanted = sorted(set(codes))
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
    added them. A removed row is gone, so the change itself is logged, once
    it commits, with the scope before and after and who made it.

    Only a field the descriptors list can be given a scope; any name can be
    made base, which is how rows left behind by a removed field are cleared.

    Edits to one field are serialised: two saves read-then-write the same
    rows, and without the lock they would leave the union of both scopes.
    """
    from omop_core.models import FieldDiseaseMembership
    from omop_core.services.field_mapping_provenance import CURATOR_PROVENANCE, user_provenance

    wanted = clean_codes(codes)
    if wanted and field_name not in field_names():
        raise ScopeError(f'{field_name!r} is not a PatientRecord field that can have a scope.')
    label = provenance or (user_provenance(user) if user is not None else CURATOR_PROVENANCE)
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                               [f'field_disease_membership:{field_name}'])
        rows = FieldDiseaseMembership.objects.filter(field_name=field_name)
        before = sorted(rows.values_list('disease', flat=True))
        if before == wanted:
            return wanted
        rows.exclude(disease__in=wanted).delete()
        FieldDiseaseMembership.objects.bulk_create([
            FieldDiseaseMembership(field_name=field_name, disease=code, provenance=label,
                                   created_by=user if getattr(user, 'pk', None) else None)
            for code in wanted if code not in before
        ])
        # After the commit, so a change an outer transaction rolls back is
        # not reported as made.
        who = getattr(user, 'pk', None) or label
        transaction.on_commit(lambda: logger.info(
            'Field scope changed: field=%s before=%s after=%s by=%s', field_name, before, wanted, who))
    return wanted


def seed() -> int:
    """Write the known extensions, as the migrations do on a deployed instance.

    For a database built without migrations (pytest). The migrations carry
    their own frozen copy of the rows and never import this module; a test
    holds ``SEED_DISEASE_FIELDS`` equal to what they write. Additive, and a
    category the instance does not have is skipped rather than given members.
    """
    from omop_core.models import DiseaseCategoryChoice, FieldDiseaseMembership

    known = set(DiseaseCategoryChoice.objects.values_list('code', flat=True))
    held = set(FieldDiseaseMembership.objects.values_list('field_name', 'disease'))
    rows = [
        FieldDiseaseMembership(field_name=field_name, disease=code, provenance=SEED_PROVENANCE)
        for code, fields in sorted(SEED_DISEASE_FIELDS.items())
        if not known or code in known
        for field_name in sorted(fields)
        if (field_name, code) not in held
    ]
    FieldDiseaseMembership.objects.bulk_create(rows)
    return len(rows)


def profile_codes(values: Iterable[str]) -> set[str]:
    """The diseases a ``?disease=`` filter names, as extension codes.

    Accepts a Disease Category code in any case, or a disease name that
    resolves to one. ``Other``, and a name mapped to it, is accepted and names
    no extension. Blank values are dropped. Raises ``ScopeError`` for anything
    else.
    """
    resolve = resolver()
    codes = set()
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        code = resolve(text)
        if code is None:
            raise ScopeError(f'Unknown disease {text!r}. Choose from the Disease Categories: '
                             f'{", ".join(sorted(category_codes()))}.')
        codes.add(code)
    return codes - {OTHER}


def annotate(descriptor: dict[str, dict]) -> dict[str, dict]:
    """A ``{field: entry}`` descriptor with ``scope`` and ``diseases`` on each entry.

    A field's scope is its own rows, here as in the field-mapping descriptors.
    The entries are copied: the builder's own dicts are left as they were.
    """
    scoped = memberships()
    return {
        field_name: {**entry, 'scope': scope_of(scoped.get(field_name, [])),
                     'diseases': list(scoped.get(field_name, []))}
        for field_name, entry in descriptor.items()
    }


def profile(descriptor: dict[str, dict], codes: Iterable[str]) -> dict[str, dict]:
    """The entries of an annotated descriptor that are part of ``codes``' profile."""
    codes = set(codes)
    return {field_name: entry for field_name, entry in descriptor.items()
            if in_profile(entry['diseases'], codes)}
