"""SNOMED CT candidates judged by hierarchy, defining attributes and subsumption.

SNOMED has no fixed six-part name like LOINC. A concept is defined by its
top-level hierarchy (the semantic tag: disorder, finding, procedure, ...) and
by defining attributes from the SNOMED Concept Model, which differ by
hierarchy -- finding site and associated morphology for a disorder, method and
procedure site for a procedure. OMOP carries both: the hierarchy as
``concept_class_id``, the attributes as ``concept_relationship`` rows
("Has finding site", "Has asso morph", ...), and subsumption as
``concept_ancestor``. Athena also records whether a concept is fully defined
or primitive, as a "Has status" relationship.

The rule differs from LOINC's because SNOMED has subsumption. Against the
source's evidence a candidate is:

* **the same concept, or agrees on every attribute it has** -- eligible;
* **more general** (an ancestor of what the source means) -- valid but less
  precise: eligible, ranked lower the further up it is;
* **more specific** (a descendant) -- it adds detail the source does not
  support, the commonest silent error in SNOMED mapping: not offered to the
  ranker, but listed for a curator who has the evidence;
* **contradictory** -- another hierarchy, another finding site or morphology,
  the other side, or (when the source has a reference concept) outside the
  reference's lineage altogether, like a sibling subtype: excluded.

A primitive concept lacks some defining attributes by construction, so a
missing attribute is *unknown*, never a conflict, and is marked as such.

The source's evidence, strongest first: a curator's reference concept and
settings (``SourceCodeConceptMapping.snomed_attributes``); the source code
itself when it is a standard SNOMED concept; the SNOMED concept its source
concept "Maps to" (how ICD-10-CM arrives). Laterality is read from the source
text as well, because Athena often maps a lateral code to an unlateralised
concept (ICD-10-CM M17.11, right knee, maps to "Osteoarthritis of knee"); a
concept that cannot carry it is kept and flagged, so the side is recorded
elsewhere rather than lost in a wrong concept.
"""
import re
from collections import defaultdict

#: Defining attributes per hierarchy (concept class), as OMOP relationship ids.
_FINDING = (
    ('Has finding site', 'Finding site'),
    ('Has asso morph', 'Morphology'),
    ('Has causative agent', 'Causative agent'),
    ('Has pathology', 'Pathological process'),
    ('Has occurrence', 'Occurrence'),
    ('Has clinical course', 'Clinical course'),
    ('Has severity', 'Severity'),
    ('Has episodicity', 'Episodicity'),
    ('Has due to', 'Due to'),
    ('Has interprets', 'Interprets'),
    ('Has interpretation', 'Interpretation'),
)
ATTRIBUTES = {
    'Disorder': _FINDING,
    'Clinical Finding': _FINDING,
    'Procedure': (
        ('Has method', 'Method'),
        ('Has dir proc site', 'Direct site'),
        ('Has indir proc site', 'Indirect site'),
        ('Has proc site', 'Procedure site'),
        ('Has dir morph', 'Direct morphology'),
        ('Has proc morph', 'Morphology'),
        ('Has dir device', 'Device'),
        ('Has proc device', 'Device'),
        ('Has approach', 'Approach'),
    ),
    'Observable Entity': (
        ('Has component', 'Component'),
        ('Has property type', 'Property'),
        ('Has time aspect', 'Time aspect'),
        ('Has inherent loc', 'Inheres in'),
        ('Has direct site', 'Direct site'),
        ('Has scale type', 'Scale'),
        ('Has technique', 'Technique'),
    ),
}
ATTRIBUTE_LABELS = {rel: label for attrs in ATTRIBUTES.values() for rel, label in attrs}
#: Attributes whose values are body structures, which carry laterality.
SITE_ATTRIBUTES = {'Has finding site', 'Has dir proc site', 'Has indir proc site',
                   'Has proc site', 'Has direct site', 'Has inherent loc'}
HIERARCHY_TAGS = {
    'Disorder': 'disorder', 'Clinical Finding': 'finding', 'Procedure': 'procedure',
    'Observable Entity': 'observable entity', 'Body Structure': 'body structure',
    'Substance': 'substance', 'Organism': 'organism', 'Morph Abnormality': 'morphologic abnormality',
    'Context-dependent': 'situation', 'Event': 'event', 'Qualifier Value': 'qualifier value',
}
LATERALITIES = ('Left', 'Right', 'Right and left')
#: Domains whose destinations are commonly SNOMED.
SNOMED_DOMAINS = {'Condition', 'Procedure', 'Observation', 'Measurement'}

MATCH, GENERAL, SPECIFIC, CONFLICT, UNKNOWN = 'match', 'general', 'specific', 'conflict', 'unknown'
EXACT = 'exact'
REASON_CONFLICT, REASON_SPECIFIC = 'conflict', 'more_specific'
BASIS_CURATOR, BASIS_SOURCE_CODE, BASIS_MAPS_TO, BASIS_TEXT = 'curator', 'source_code', 'maps_to', 'text'
RETRIEVAL_HIERARCHY = 'hierarchy'
#: How far up from a reference concept retrieval walks for general candidates.
HIERARCHY_RETRIEVAL_LEVELS = 3
HIERARCHY_RETRIEVAL_LIMIT = 10

_LEFT = re.compile(r'\b(left|lt)\b', re.I)
_RIGHT = re.compile(r'\b(right|rt)\b', re.I)
_BOTH = re.compile(r'\b(bilateral|both sides|right and left|left and right)\b', re.I)


def laterality_in_text(*texts):
    """``'Left'``, ``'Right'``, ``'Right and left'`` or ``None`` from free text."""
    text = ' '.join(t for t in texts if t)
    if not text:
        return None
    if _BOTH.search(text):
        return 'Right and left'
    left, right = bool(_LEFT.search(text)), bool(_RIGHT.search(text))
    if left and right:
        return 'Right and left'
    return 'Left' if left else 'Right' if right else None


# --- reading SNOMED from the database ------------------------------------------

def concept_facts(concept_ids):
    """``{concept_id: facts}`` for SNOMED concepts, in three queries.

    ``facts`` holds ``hierarchy`` (concept class), ``status`` (``Defined`` /
    ``Primitive`` / ``None``), ``attributes`` (``{relationship_id:
    [(value_id, value_name)]}``) and ``laterality`` (from site values).
    """
    from omop_core.models import Concept, ConceptRelationship

    concept_ids = {cid for cid in concept_ids if cid}
    if not concept_ids:
        return {}
    facts = {
        c['concept_id']: {'hierarchy': c['concept_class_id'], 'status': None,
                          'attributes': defaultdict(list), 'laterality': None}
        for c in Concept.objects.filter(concept_id__in=concept_ids, vocabulary_id='SNOMED')
                                .values('concept_id', 'concept_class_id')
    }
    if not facts:
        return {}
    wanted = set(ATTRIBUTE_LABELS) | {'Has status'}
    rows = (ConceptRelationship.objects
            .filter(concept_1_id__in=facts, relationship_id__in=wanted, invalid_reason__isnull=True)
            .order_by('concept_1_id', 'relationship_id', 'concept_2_id')
            .values_list('concept_1_id', 'relationship_id', 'concept_2_id', 'concept_2__concept_name'))
    sites = defaultdict(set)
    sides = defaultdict(set)
    for cid, rel, value_id, value_name in rows:
        if rel == 'Has status':
            facts[cid]['status'] = value_name
            continue
        facts[cid]['attributes'][rel].append((value_id, value_name))
        if rel in SITE_ATTRIBUTES:
            sites[value_id].add(cid)
    if sites:
        for site_id, side in (ConceptRelationship.objects
                              .filter(concept_1_id__in=sites, relationship_id='Has laterality',
                                      invalid_reason__isnull=True)
                              .values_list('concept_1_id', 'concept_2__concept_name')):
            # "Side" marks a structure that *can* be lateralised; only an
            # actual side is a laterality.
            if side not in LATERALITIES:
                continue
            for cid in sites[site_id]:
                sides[cid].add(side)
    # A concept sited on both a left and a right structure is bilateral.
    for cid, found in sides.items():
        facts[cid]['laterality'] = (
            'Right and left' if 'Right and left' in found or {'Left', 'Right'} <= found
            else found.pop() if len(found) == 1 else 'Right and left')
    for fact in facts.values():
        fact['attributes'] = dict(fact['attributes'])
    return facts


def ancestry(concept_ids):
    """``{(ancestor, descendant): levels}`` among ``concept_ids``, one query."""
    from omop_core.models import ConceptAncestor

    concept_ids = {cid for cid in concept_ids if cid}
    if len(concept_ids) < 2:
        return {}
    return {
        (a, d): levels for a, d, levels in
        ConceptAncestor.objects.filter(ancestor_concept_id__in=concept_ids,
                                       descendant_concept_id__in=concept_ids)
        .exclude(min_levels_of_separation=0)
        .values_list('ancestor_concept_id', 'descendant_concept_id', 'min_levels_of_separation')
    }


# --- the source side ------------------------------------------------------------

def _standard_snomed(concept):
    return (concept is not None and concept.vocabulary_id == 'SNOMED'
            and concept.standard_concept == 'S' and not concept.invalid_reason)


def reference_concepts(source_concept):
    """``(concepts, basis)``: the SNOMED concepts the source code itself means."""
    from omop_core.models import Concept

    if source_concept is None:
        return [], None
    if _standard_snomed(source_concept):
        return [source_concept], BASIS_SOURCE_CODE
    mapped = list(Concept.objects.filter(
        concept_id__in=_maps_to_ids(source_concept.pk),
        vocabulary_id='SNOMED', standard_concept='S', invalid_reason__isnull=True,
    ).order_by('concept_id'))
    return (mapped, BASIS_MAPS_TO) if mapped else ([], None)


def _maps_to_ids(concept_id):
    from omop_core.models import ConceptRelationship

    return list(ConceptRelationship.objects.filter(
        concept_1_id=concept_id, relationship_id='Maps to', invalid_reason__isnull=True,
    ).values_list('concept_2_id', flat=True))


def clean_overrides(raw):
    """Validate a curator's SNOMED settings; raises ``ValueError``."""
    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise ValueError('snomed_attributes must be an object.')
    unknown = set(raw) - {'reference_concept_id', 'hierarchy', 'laterality'}
    if unknown:
        raise ValueError(f'Unknown SNOMED settings: {", ".join(sorted(unknown))}.')
    cleaned = {}
    if raw.get('reference_concept_id') not in (None, ''):
        try:
            cleaned['reference_concept_id'] = int(raw['reference_concept_id'])
        except (TypeError, ValueError):
            raise ValueError('reference_concept_id must be a concept id.') from None
    if 'hierarchy' in raw:
        if raw['hierarchy'] not in (None, '') and raw['hierarchy'] not in HIERARCHY_TAGS:
            raise ValueError(f'hierarchy must be one of {", ".join(HIERARCHY_TAGS)}.')
        cleaned['hierarchy'] = raw['hierarchy'] or None
    if 'laterality' in raw:
        side = raw['laterality']
        if side not in (None, '', 'none') and side not in LATERALITIES:
            raise ValueError(f'laterality must be one of {", ".join(LATERALITIES)}, or none.')
        # "none" is the spelled-out form of null: no side, not a side named "none".
        cleaned['laterality'] = None if side in (None, '', 'none') else side
    return cleaned


def resolve_source(*, source_concept, description='', metadata=None, overrides=None):
    """The source's SNOMED evidence.

    Returns ``{'references': [{concept_id, concept_name, concept_code}],
    'reference_basis', 'hierarchy': {value, basis, evidence} | None,
    'laterality': {value, basis, evidence} | None, 'attributes':
    {relationship_id: [(value_id, value_name)]}}``.
    """
    from omop_core.models import Concept

    overrides = overrides or {}
    references, basis = [], None
    if overrides.get('reference_concept_id'):
        chosen = Concept.objects.filter(pk=overrides['reference_concept_id']).first()
        if _standard_snomed(chosen):
            references, basis = [chosen], BASIS_CURATOR
    if not references:
        references, basis = reference_concepts(source_concept)
    facts = concept_facts([c.pk for c in references])

    hierarchy = None
    if 'hierarchy' in overrides:
        if overrides['hierarchy']:
            hierarchy = {'value': overrides['hierarchy'], 'basis': BASIS_CURATOR, 'evidence': 'Set by a curator'}
    else:
        classes = {facts[c.pk]['hierarchy'] for c in references if c.pk in facts}
        if len(classes) == 1:
            value = classes.pop()
            hierarchy = {'value': value, 'basis': basis,
                         'evidence': f'{_basis_words(basis)} {references[0].concept_name}'}

    laterality = None
    if 'laterality' in overrides:
        if overrides['laterality']:
            laterality = {'value': overrides['laterality'], 'basis': BASIS_CURATOR, 'evidence': 'Set by a curator'}
    else:
        texts = [description]
        if source_concept is not None:
            texts.append(source_concept.concept_name)
        resource = (metadata or {}).get('fhir', metadata) if isinstance(metadata, dict) else {}
        code = resource.get('code') if isinstance(resource, dict) else None
        if isinstance(code, dict):
            texts.append(code.get('text') or '')
        side = laterality_in_text(*texts)
        if side:
            laterality = {'value': side, 'basis': BASIS_TEXT, 'evidence': 'Named in the source text'}
        else:
            sides = {facts[c.pk]['laterality'] for c in references if c.pk in facts} - {None}
            if len(sides) == 1:
                laterality = {'value': sides.pop(), 'basis': basis,
                              'evidence': f'{_basis_words(basis)} {references[0].concept_name}'}

    attributes = defaultdict(list)
    for concept in references:
        for rel, values in facts.get(concept.pk, {}).get('attributes', {}).items():
            for value in values:
                if value not in attributes[rel]:
                    attributes[rel].append(value)
    return {
        'references': [{'concept_id': c.pk, 'concept_name': c.concept_name,
                        'concept_code': c.concept_code,
                        'hierarchy': facts.get(c.pk, {}).get('hierarchy')} for c in references],
        'reference_basis': basis,
        'hierarchy': hierarchy,
        'laterality': laterality,
        'attributes': dict(attributes),
    }


def _basis_words(basis):
    return {BASIS_SOURCE_CODE: 'Source code is', BASIS_MAPS_TO: 'Source code maps to',
            BASIS_CURATOR: 'Curator chose'}.get(basis, '')


def has_evidence(source):
    return bool(source and (source['references'] or source['hierarchy'] or source['laterality']))


def source_summary(source):
    """The source evidence as a curator and the ranker see it."""
    return {
        'references': source['references'],
        'reference_basis': source['reference_basis'],
        'hierarchy': source['hierarchy'],
        'laterality': source['laterality'],
        'attributes': [
            {'relationship_id': rel, 'label': ATTRIBUTE_LABELS.get(rel, rel),
             'values': [name for _id, name in values]}
            for rel, values in source['attributes'].items()
        ],
    }


# --- comparing ------------------------------------------------------------------

def _value_verdict(source_values, candidate_values, lineage):
    if not source_values or not candidate_values:
        return UNKNOWN
    source_ids = {vid for vid, _ in source_values}
    candidate_ids = {vid for vid, _ in candidate_values}
    if source_ids & candidate_ids:
        return MATCH
    if any((c, s) in lineage for c in candidate_ids for s in source_ids):
        return GENERAL
    if any((s, c) in lineage for c in candidate_ids for s in source_ids):
        return SPECIFIC
    return CONFLICT


def apply(candidates, source):
    """Annotate, filter and order SNOMED candidates against the source evidence.

    Returns ``(eligible, excluded)``. Non-SNOMED candidates pass through
    untouched. Each SNOMED candidate gets ``snomed``: its hierarchy, status,
    attributes with a verdict each, laterality, and its subsumption relation
    to the reference (``exact`` / ``general`` / ``specific`` with ``levels``).
    Excluded candidates carry ``exclusion_reason`` (``conflict`` or
    ``more_specific``) and ``snomed_conflicts``.
    """
    snomed = [c for c in candidates if c.get('vocabulary_id') == 'SNOMED']
    if not snomed:
        return list(candidates), []
    describe_only = not has_evidence(source)
    reference_ids = {r['concept_id'] for r in source['references']}
    facts = concept_facts([c['concept_id'] for c in snomed])
    involved = set(reference_ids) | {c['concept_id'] for c in snomed}
    for fact in facts.values():
        for values in fact['attributes'].values():
            involved |= {vid for vid, _ in values}
    for values in source['attributes'].values():
        involved |= {vid for vid, _ in values}
    lineage = ancestry(involved)

    source_hierarchy = (source['hierarchy'] or {}).get('value')
    source_side = (source['laterality'] or {}).get('value')
    # A map often drops the side (ICD-10-CM "right knee" -> "Osteoarthritis
    # of knee"). A child one level below an unlateralised reference that
    # differs by exactly the side the source names adds nothing the source
    # does not say, so it counts as the reference -- and its ancestors as
    # general candidates.
    reference_sides = {facts.get(r, {}).get('laterality') for r in reference_ids}
    lateralised = set()
    side_restored = bool(source_side and reference_ids and reference_sides <= {None})
    if side_restored:
        lateralised = {
            c['concept_id'] for c in snomed
            if c['concept_id'] in facts and facts[c['concept_id']]['laterality'] == source_side
            and any(lineage.get((r, c['concept_id'])) == 1 for r in reference_ids)
        }
    supported = set(reference_ids) | lateralised
    eligible, excluded = [], []
    for position, candidate in enumerate(candidates):
        if candidate.get('vocabulary_id') != 'SNOMED':
            eligible.append((position, candidate, (1, 1, 0, 99)))
            continue
        fact = facts.get(candidate['concept_id'])
        if fact is None:
            eligible.append((position, candidate, (1, 1, 0, 99)))
            continue
        cid = candidate['concept_id']
        relation, levels = None, None
        if cid in supported:
            relation, levels = EXACT, 0
        else:
            ups = [lineage[(cid, r)] for r in supported if (cid, r) in lineage]
            downs = [lineage[(r, cid)] for r in supported if (r, cid) in lineage]
            if ups:
                relation, levels = GENERAL, min(ups)
            elif downs:
                relation, levels = SPECIFIC, min(downs)

        attributes, conflicts = [], []
        for rel, label in ATTRIBUTES.get(fact['hierarchy'], ()):
            candidate_values = fact['attributes'].get(rel, [])
            verdict = _value_verdict(source['attributes'].get(rel, []), candidate_values, lineage)
            if verdict == UNKNOWN and not candidate_values and not source['attributes'].get(rel):
                continue
            if verdict == CONFLICT and relation in (EXACT, GENERAL):
                verdict = GENERAL  # an ancestor's attribute subsumes the source's
            if (verdict == SPECIFIC and rel in SITE_ATTRIBUTES and side_restored
                    and fact['laterality'] == source_side):
                verdict = MATCH  # the narrower site is the side the source names
            attributes.append({'relationship_id': rel, 'label': label,
                               'values': [name for _id, name in candidate_values], 'verdict': verdict,
                               **({'primitive_gap': True} if verdict == UNKNOWN and not candidate_values
                                  and fact['status'] == 'Primitive' else {})})
            if verdict == CONFLICT:
                conflicts.append(label)
        if source_hierarchy and fact['hierarchy'] != source_hierarchy:
            conflicts.insert(0, 'Hierarchy')
        elif reference_ids and relation is None:
            # Neither the reference, an ancestor nor a descendant: a sibling
            # or a stranger is another concept, whatever attributes it shares.
            conflicts.insert(0, 'Lineage')
        side = fact['laterality']
        if source_side and side and side != source_side:
            conflicts.append('Laterality')
        annotated = {**candidate, 'snomed': {
            'hierarchy': fact['hierarchy'],
            'semantic_tag': HIERARCHY_TAGS.get(fact['hierarchy'], (fact['hierarchy'] or '').lower()),
            'status': fact['status'],
            'attributes': attributes,
            'laterality': side,
            'relation': relation,
            'levels': levels,
            **({'lateralised': True} if cid in lateralised else {}),
            **({'laterality_not_captured': source_side} if source_side and not side else {}),
        }}
        matched = sum(a['verdict'] == MATCH for a in attributes)
        specific = relation == SPECIFIC or any(a['verdict'] == SPECIFIC for a in attributes)
        if conflicts:
            excluded.append({**annotated, 'exclusion_reason': REASON_CONFLICT, 'snomed_conflicts': conflicts})
        elif specific and reference_ids:
            excluded.append({**annotated, 'exclusion_reason': REASON_SPECIFIC, 'snomed_conflicts': []})
        else:
            # Most specific supported first: exact, then the one that keeps
            # the source's side, then attribute matches, then the nearest
            # ancestor.
            rank = (0 if relation == EXACT else 1,
                    1 if source_side and not side else 0, -matched,
                    levels if relation == GENERAL else 99)
            eligible.append((position, annotated, rank))
    if describe_only:
        # Nothing to judge against: describe each SNOMED candidate and keep
        # the order the earlier stages (retrieval, LOINC axes) set.
        return [candidate for _, candidate, _ in eligible], excluded
    eligible.sort(key=lambda item: (item[2], item[0]))
    return [candidate for _, candidate, _ in eligible], excluded


# --- retrieval ------------------------------------------------------------------

def hierarchy_candidates(source, domain_id, limit=HIERARCHY_RETRIEVAL_LIMIT):
    """The reference concepts and their near ancestors, in the source's domain."""
    from omop_core.models import Concept, ConceptAncestor

    reference_ids = [r['concept_id'] for r in source['references']]
    if not reference_ids:
        return []
    ancestors = dict(
        ConceptAncestor.objects.filter(
            descendant_concept_id__in=reference_ids,
            min_levels_of_separation__lte=HIERARCHY_RETRIEVAL_LEVELS,
        ).order_by('min_levels_of_separation')
        .values_list('ancestor_concept_id', 'min_levels_of_separation')
    )
    ids = set(reference_ids) | set(ancestors)
    concepts = Concept.objects.filter(concept_id__in=ids, vocabulary_id='SNOMED',
                                      standard_concept='S', invalid_reason__isnull=True)
    if domain_id:
        concepts = concepts.filter(domain_id=domain_id)
    ordered = sorted(concepts, key=lambda c: (ancestors.get(c.concept_id, 0), c.concept_name))
    return [
        {
            'concept_id': c.concept_id, 'concept_name': c.concept_name,
            'concept_code': c.concept_code, 'vocabulary_id': c.vocabulary_id,
            'concept_class_id': c.concept_class_id, 'domain_id': c.domain_id,
            'standard_concept': c.standard_concept, 'retrieval': RETRIEVAL_HIERARCHY,
        }
        for c in ordered[:limit]
    ]
