"""Contract invariants for the editability descriptor (#2396).

These are producer-side gates: a client reads this mapping to decide what it may
draw and where a write lands. A shape that only looks plausible — an alias that
points at another alias, an answerable entry without a labelled action, an
input nobody can resolve — is a silent read-only row in a federated trial, so
the tests state the rules instead of trusting each block to remember them.
"""
import pytest
from django.conf import settings

from omop_core.services.patient_form import FORM_LABELS, patient_form_descriptors
from omop_core.services.write_descriptor import (
    DESCRIPTOR_META_KEY,
    DESCRIPTOR_RESOURCE_INPUTS,
    DESCRIPTOR_SCHEMA_VERSION,
    KIND_ALIAS,
    KIND_COMPUTED,
    KIND_EXPLICIT_ANSWER,
    KIND_NEGATED,
    KIND_UNMAPPED,
    build_writable_field_descriptor,
)

pytestmark = pytest.mark.django_db

NOT_AUTHORABLE = {KIND_ALIAS, KIND_COMPUTED, KIND_EXPLICIT_ANSWER, KIND_NEGATED, KIND_UNMAPPED}


@pytest.fixture
def descriptor():
    return build_writable_field_descriptor()


def fields(descriptor):
    return {name: entry for name, entry in descriptor.items()
            if not name.startswith('__')}


def test_the_schema_version_is_pinned():
    # Independent of the envelope: a bump is a deliberate contract change, so
    # this literal moves in the same commit as the clients that understand it.
    assert DESCRIPTOR_SCHEMA_VERSION == 1


def test_the_envelope_pins_the_schema_version(descriptor):
    assert descriptor[DESCRIPTOR_META_KEY] == {
        'schema_version': DESCRIPTOR_SCHEMA_VERSION,
        'documentation': 'docs/writable-field-descriptor.md',
    }


def test_the_envelope_is_not_a_field(descriptor):
    reserved = [name for name in descriptor if name.startswith('__')]
    assert reserved == [DESCRIPTOR_META_KEY]
    assert 'kind' not in descriptor[DESCRIPTOR_META_KEY]
    # No field may claim the reserved name either.
    assert DESCRIPTOR_META_KEY not in fields(descriptor)


def test_every_field_entry_carries_the_core_keys(descriptor):
    # The envelope is the only entry without them; that is what makes it safe
    # for a client to treat everything else uniformly.
    for name, entry in fields(descriptor).items():
        assert 'kind' in entry, name
        assert 'writable' in entry, name


def test_the_document_the_envelope_points_at_exists(descriptor):
    path = settings.BASE_DIR / descriptor[DESCRIPTOR_META_KEY]['documentation']
    assert path.is_file(), f'{path} is missing; the envelope points at it'


def test_form_enrichment_does_not_turn_the_envelope_into_a_field(descriptor):
    enriched = patient_form_descriptors(descriptor)
    assert DESCRIPTOR_META_KEY not in enriched
    # The enrichment still works on real fields.
    assert enriched['hemoglobin_g_dl']['label']
    assert enriched['hemoglobin_g_dl']['control']


def test_the_clinical_dates_keep_their_reviewed_labels(descriptor):
    """#2601: the fallback title-cases the verbose name, acronyms included.

    The date and its precision companion are read from the same response, and
    the fallback would spell them "Sct date" and "Sct date precision": the
    reviewed spelling is what CB derives its own companion labels from.
    """
    enriched = patient_form_descriptors(descriptor)
    assert enriched['sct_date']['label'] == 'SCT date'
    assert enriched['sct_date_precision']['label'] == 'SCT date precision'
    assert enriched['dlbcl_transformation_date']['label'] == 'DLBCL transformation date'
    assert (enriched['dlbcl_transformation_date_precision']['label']
            == 'DLBCL transformation date precision')


def test_every_reviewed_label_is_the_served_label(descriptor):
    """`FORM_LABELS` is the single owner of the wording a reader sees.

    Asserting the enriched descriptor catches an entry that names a field the
    descriptor does not serve, and one that a later override silently wins.
    """
    enriched = patient_form_descriptors(descriptor)
    for name, (label, _unit) in FORM_LABELS.items():
        assert name in enriched, f'{name} is labelled but not served'
        assert enriched[name]['label'] == label, name


# The lowercase token as it appears in a field name, and the spelling a reader
# must see. `capitalize()` only upper-cases the first character, so the fallback
# renders "Sct eligibility", "Pd l1 assay" and "Transformed to dlbcl" (#2652).
ACRONYM_SPELLINGS = {
    'sct': 'SCT', 'dlbcl': 'DLBCL', 'pd': 'PD-L1', 'egfr': 'eGFR',
    'hba1c': 'HbA1c', 'inr': 'INR', 'ptt': 'PTT', 'psa': 'PSA',
    'flipi': 'FLIPI', 'gelf': 'GELF', 'hrd': 'HRD', 'tnbc': 'TNBC',
    'btk': 'BTK', 'bcl2': 'BCL2', 'anc': 'ANC', 'alc': 'ALC', 'amc': 'AMC',
    'rbc': 'RBC', 'ast': 'AST', 'alt': 'ALT', 'bnp': 'BNP', 'cea': 'CEA',
    'bmi': 'BMI', 'mrd': 'MRD', 'her2': 'HER2', 'ecog': 'ECOG',
}


def test_no_acronym_is_served_with_the_fallback_spelling(descriptor):
    """A new acronym column must be given a reviewed label, not inherit one.

    The issue names one instance (#2601); this pins the class, so the next
    `pd_l1_*`-style field fails here instead of reaching a client flattened.
    """
    enriched = patient_form_descriptors(descriptor)
    offenders = []
    for name, entry in enriched.items():
        label = entry.get('label') or ''
        for token in name.split('_'):
            spelling = ACRONYM_SPELLINGS.get(token)
            if spelling and spelling not in label:
                offenders.append(f'{name}: {token!r} served as {label!r}')
    assert not offenders, offenders


def test_aliases_are_one_way(descriptor):
    by_name = fields(descriptor)
    for name, entry in by_name.items():
        if entry.get('kind') != KIND_ALIAS:
            continue
        canonical = entry.get('canonical')
        assert isinstance(canonical, str) and canonical, f'{name} names no canonical'
        assert canonical in by_name, f'{name} mirrors missing {canonical}'
        assert canonical != name, f'{name} mirrors itself'
        assert by_name[canonical].get('kind') != KIND_ALIAS, (
            f'{name} -> {canonical} is a second alias hop; canonical is the only write target'
        )
        assert entry.get('writable') is False


def test_the_documented_canonical_exceptions_hold(descriptor):
    by_name = fields(descriptor)
    # The trial catalog's legacy spelling; the record and serializer use the
    # second "t" (#755).
    assert by_name['cytogenic_markers']['kind'] == KIND_ALIAS
    assert by_name['cytogenic_markers']['canonical'] == 'cytogenetic_markers'
    # Race and ethnicity are separate OMOP concepts, and neither aliases the
    # other (cancerbot-org/cancerbot#5777).
    assert by_name['ethnicity'].get('kind') != KIND_ALIAS
    assert by_name['race'].get('kind') != KIND_ALIAS
    assert by_name.get('ethnicity', {}).get('canonical') is None
    assert by_name.get('race', {}).get('canonical') is None


def test_writable_never_describes_a_derived_or_answered_field(descriptor):
    for name, entry in fields(descriptor).items():
        if entry.get('writable') is not True:
            continue
        assert entry.get('kind') not in NOT_AUTHORABLE, (
            f'{name} claims a direct write while its kind is {entry.get("kind")}'
        )


def test_answerable_means_an_explicit_answer_with_labelled_actions(descriptor):
    answerable = [entry for entry in fields(descriptor).values()
                  if entry.get('answerable')]
    assert answerable, 'the explicit pre-existing conditions answer disappeared'
    for entry in answerable:
        assert entry.get('kind') == KIND_EXPLICIT_ANSWER
        assert entry.get('writable') is False
        assert entry.get('endpoint')
        actions = entry.get('actions')
        assert isinstance(actions, list) and actions
        labels = entry.get('action_labels')
        assert isinstance(labels, dict)
        # A client drops an unlabelled action rather than inventing clinical
        # wording, so an incomplete set silently empties the dialog.
        assert set(actions) <= set(labels)
        assert all(isinstance(labels[action], str) and labels[action]
                   for action in actions)


def test_options_are_well_formed(descriptor):
    for name, entry in fields(descriptor).items():
        options = entry.get('options')
        if options is None:
            continue
        assert isinstance(options, list), f'{name}.options is not a list'
        for option in options:
            assert isinstance(option, dict), f'{name} has a non-object option'
            value = option.get('value')
            assert value is not None and value != '', (
                f'{name} has an option with no value'
            )


def test_inputs_resolve_to_a_field_or_a_declared_resource(descriptor):
    by_name = fields(descriptor)
    for name, entry in by_name.items():
        for source in entry.get('inputs') or []:
            assert source in by_name or source in DESCRIPTOR_RESOURCE_INPUTS, (
                f'{name} names input {source!r}, which is neither a descriptor '
                'field nor a declared resource'
            )


def test_a_computed_value_names_what_it_is_computed_from(descriptor):
    by_name = fields(descriptor)
    for name, entry in by_name.items():
        if entry.get('kind') != KIND_COMPUTED:
            continue
        # A computed value with no inputs can only be a normalised read model
        # of a source pair or an assembled display value; those carry their own
        # marker. Everything else has to navigate somewhere.
        assert entry.get('inputs') or entry.get('normalised_read_model') \
            or entry.get('compatibility_scope') or entry.get('reason'), (
            f'{name} is computed with no inputs and no explanation'
        )
