"""Which controlled lookup a field's options come from (#1225).

Nineteen fields on the patient tabs offer the entries of a ``VocabularyLookup``
table, and until now only the frontend knew which: ``useVocabulary('her2-status')``
beside the field. This is the server's record of it, so the choice editor can
show a curator what a field offers without a second, editable copy of it. It
only reads: the lookup tables stay managed where they are loaded.

A field is bound in one of two ways:

* **enforced**: its approved ``FieldConceptMapping`` names the lookup in
  ``value_vocabulary`` and is complete enough to write (a concept and a table).
  The write descriptor then offers only those titles, so answers curated for the
  field are not offered. (The two transplant fields also have serializer and
  upload validators that keep only the lookup's titles, whatever the mapping.)
* **offered**: the tab draws its dropdown from the lookup. A field with an
  approved concept mapping and curated answers offers those instead, since the
  write descriptor's options win over the tab's own.

Keep ``LOOKUP_FIELDS`` in step with the tabs: ``tests/test_option_sources.py``
fails when a ``useVocabulary`` call names a lookup no field here uses.

**A decision about one entry** (#1225) is recorded on a ``FieldChoice`` made when
a curator first decides something for it: its own context (``lookup:<slug>``),
the entry's title as its label and the value the tab stores as its value, so the
whole of ``field_value_mappings`` (propose, approve, revisions, projection,
readback, transfer) works on it unchanged. Such a row is never offered as an
answer (``is_lookup_context`` keeps it out of the dropdown, the descriptors and
the answer coverage) and nothing but ``ensure_answer`` makes one, so it is not a
second list: it holds a decision and is found again by the entry's title.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

#: The marker a lookup-backed answer's ``context_key`` starts with.
LOOKUP_CONTEXT_PREFIX = 'lookup:'

VOCABULARY_MODELS: dict[str, str] = {
    'ethnicity': 'Ethnicity',
    'stem-cell-transplant': 'StemCellTransplant',
    'sct-eligibility': 'SctEligibility',
    'histologic-type': 'HistologicType',
    'estrogen-receptor-status': 'EstrogenReceptorStatus',
    'progesterone-receptor-status': 'ProgesteroneReceptorStatus',
    'her2-status': 'Her2Status',
    'hr-status': 'HrStatus',
    'hrd-status': 'HrdStatus',
    'mutation-origin': 'MutationOrigin',
    'mutation-gene': 'MutationGene',
    'mutation-interpretation': 'MutationInterpretation',
    'mutation-code': 'MutationCode',
    'tumor-stage': 'TumorStage',
    'nodes-stage': 'NodesStage',
    'distant-metastasis-stage': 'DistantMetastasisStage',
    'staging-modality': 'StagingModality',
    'toxicity-grade': 'ToxicityGrade',
    'language': 'Language',
    'language-skill-level': 'LanguageSkillLevel',
    'binet-stage': 'BinetStage',
    'protein-expression': 'ProteinExpression',
    'richter-transformation': 'RichterTransformation',
    'tumor-burden': 'TumorBurden',
    'morphologic-variant': 'MorphologicVariant',
    'disease-activity': 'DiseaseActivity',
    'pre-existing-condition-category': 'PreExistingConditionCategory',
    'disease': 'Disease',
    'cancer-stage': 'CancerStage',
    'karnofsky-score': 'KarnofskyScore',
    'ecog-status': 'EcogStatus',
    'peripheral-neuropathy-grade': 'PeripheralNeuropathyGrade',
    'infection-status': 'InfectionStatus',
    'disease-progression': 'DiseaseProgression',
    'measurable-disease': 'MeasurableDisease',
    'gelf-criteria': 'GelfCriteria',
    'flipi-score': 'FlipIScore',
    'follicular-lymphoma-grade': 'FollicularLymphomaGrade',
    'post-transformation-outcome': 'PostTransformationOutcome',
    'breast-cancer-first-line-therapy': 'BreastCancerFirstLineTherapy',
    'breast-cancer-second-line-therapy': 'BreastCancerSecondLineTherapy',
    'breast-cancer-later-line-therapy': 'BreastCancerLaterLineTherapy',
    'myeloma-type': 'MyelomaType',
    'therapy-regimen': 'TherapyRegimen',
    'therapy-component': 'TherapyComponent',
    'therapy-class': 'TherapyClass',
    'therapy-round': 'TherapyRound',
}


#: Fields whose tab stores the lookup entry's *code* (``useVocabulary(.., 'code')``);
#: every other field stores the entry's title.
CODE_VALUED: frozenset[str] = frozenset({'ecog_performance_status', 'karnofsky_performance_score'})

#: PatientRecord field -> the lookup slug served by ``/api/v1/vocabularies/<slug>/``.
LOOKUP_FIELDS: dict[str, str] = {
    'estrogen_receptor_status': 'estrogen-receptor-status',
    'progesterone_receptor_status': 'progesterone-receptor-status',
    'her2_status': 'her2-status',
    'hr_status': 'hr-status',
    'hrd_status': 'hrd-status',
    'tumor_stage': 'tumor-stage',
    'nodes_stage': 'nodes-stage',
    'distant_metastasis_stage': 'distant-metastasis-stage',
    'staging_modalities': 'staging-modality',
    'histologic_type': 'histologic-type',
    'tumor_grade': 'follicular-lymphoma-grade',
    'post_transformation_outcome': 'post-transformation-outcome',
    'progression': 'disease-progression',
    'stem_cell_transplant_history': 'stem-cell-transplant',
    'sct_eligibility': 'sct-eligibility',
    'myeloma_type': 'myeloma-type',
    'ecog_performance_status': 'ecog-status',
    'karnofsky_performance_score': 'karnofsky-score',
    'ethnicity': 'ethnicity',
}


def lookup_context(slug: str) -> str:
    return f'{LOOKUP_CONTEXT_PREFIX}{slug}'


def is_lookup_context(context_key: str) -> bool:
    return str(context_key or '').startswith(LOOKUP_CONTEXT_PREFIX)


def vocabulary_model(slug: str):
    """The lookup model served as ``slug``, or None."""
    from omop_core import models

    name = VOCABULARY_MODELS.get(slug)
    return getattr(models, name, None) if name else None


@dataclass(frozen=True)
class LookupBinding:
    model: type
    #: The slug of ``/vocabularies/<slug>/``; empty for a lookup the registry does not serve.
    slug: str
    #: The field's approved, complete mapping names this lookup, so the write
    #: descriptor offers only its titles.
    enforced: bool

    @property
    def name(self) -> str:
        return self.model.__name__


def resolve_lookup(field_name: str) -> LookupBinding | None:
    """The lookup a field's options come from, or None.

    A mapping's own ``value_vocabulary`` wins over the tabs' hint, resolved by
    class name the way the write descriptor does, whatever the registry serves.
    """
    from omop_core import models
    from omop_core.models import FieldConceptMapping, VocabularyLookup

    by_class = {name: slug for slug, name in VOCABULARY_MODELS.items()}
    row = (FieldConceptMapping.objects.filter(field_name=field_name, status='approved')
           .exclude(value_vocabulary='').first())
    model = None
    if row is not None:
        candidate = getattr(models, row.value_vocabulary, None)
        if isinstance(candidate, type) and issubclass(candidate, VocabularyLookup) and not candidate._meta.abstract:
            model = candidate
    if model is not None:
        # The descriptor offers only the lookup's titles when the row is complete
        # enough to write (a concept and a table) and the lookup has entries.
        enforced = bool(row.concept_id is not None and row.omop_table and model.objects.exists())
        return LookupBinding(model, by_class.get(model.__name__, ''), enforced)
    slug = LOOKUP_FIELDS.get(field_name)
    model = vocabulary_model(slug) if slug else None
    return LookupBinding(model, slug, False) if model is not None else None


def stored_form(field_name: str, entry) -> str:
    """What the tab stores when this entry is picked: its code or its title."""
    return str(entry.code) if field_name in CODE_VALUED else str(entry.title)


def _entry_code(entry) -> str:
    return str(entry.code)


class NoLookup(LookupError):
    """The field offers no lookup, or the lookup has no such entry."""


def ensure_answer(field_name: str, code: str, *, user=None):
    """The answer row that holds decisions about one lookup entry; made when asked for.

    Found by the entry's title in the lookup's own context, so it is the same
    row whoever asks and a renamed entry starts afresh (the old row keeps its
    decision and shows as no longer in the lookup). Raises ``NoLookup``.
    """
    from omop_core.models import FieldChoice

    binding = resolve_lookup(field_name)
    if binding is None:
        raise NoLookup('This field offers no lookup.')
    entry = binding.model.objects.filter(code=str(code)).first()
    if entry is None:
        raise NoLookup('The lookup has no such entry.')
    context = lookup_context(binding.slug or binding.name)
    found = FieldChoice.objects.filter(field_name=field_name, context_key=context, display=entry.title).first()
    if found is not None:
        return found
    stored = stored_form(field_name, entry)
    try:
        with transaction.atomic():
            return FieldChoice.objects.create(
                field_name=field_name, context_key=context, display=entry.title,
                value='' if stored == entry.title else stored, created_by=user,
            )
    except IntegrityError:
        # Two curators asked at once: the second reads the first's row.
        return FieldChoice.objects.get(field_name=field_name, context_key=context, display=entry.title)
