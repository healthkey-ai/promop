"""Reference regimens for the therapy-line editor, without host policy.

Both the in-place endpoint and the packaged one return the same payloads; only
their permissions differ. The curation writes (create/patch/delete a regimen)
stay in ``patient_portal`` because they are an administrative product feature,
not part of what an embedding host needs to render the editor.
"""
from django.db.models import Prefetch

from omop_core.models import (
    DiseaseTherapyRegimen, TherapyComponentClassLink, TherapyRegimen, TherapyRegimenComponent,
)


def regimen_list_payload(query_params):
    """Regimens filtered by disease, round and/or title search.

    A disease/round picker needs the complete available list; a search stays
    bounded, but alphabetical truncation must not hide valid supportive care.
    """
    disease_code = query_params.get('disease')
    round_code = query_params.get('round')
    search = (query_params.get('search') or '').strip()

    queryset = TherapyRegimen.objects.all().order_by('title')
    if disease_code or round_code:
        links = DiseaseTherapyRegimen.objects.all()
        if disease_code:
            from omop_core.services.treatment_catalog import disease_filter
            links = links.filter(disease_filter(disease_code))
        if round_code:
            links = links.filter(round__code=round_code)
        queryset = queryset.filter(id__in=links.values_list('regimen_id', flat=True))
    if search:
        queryset = queryset.filter(title__icontains=search)

    values = queryset.values('code', 'title', 'concept_id')
    return list(values if round_code and not search else values[:50])


def regimen_detail_payload(code):
    """One regimen with its components and their classes, or None if unknown."""
    regimen = TherapyRegimen.objects.filter(code=code).first()
    if regimen is None:
        return None
    links = (
        TherapyRegimenComponent.objects.filter(regimen=regimen)
        .select_related('component', 'component__concept')
        .prefetch_related(Prefetch(
            'component__component_classes',
            queryset=TherapyComponentClassLink.objects.select_related('therapy_class'),
        ))
    )
    components = []
    for link in links:
        component = link.component
        concept = component.concept
        components.append({
            'code': component.code,
            'title': component.title,
            'concept_id': component.concept_id,
            'concept_name': concept.concept_name if concept else component.title,
            'concept_code': concept.concept_code if concept else component.code,
            'vocabulary_id': concept.vocabulary_id if concept else None,
            'standard_concept': concept.standard_concept if concept else None,
            'invalid_reason': concept.invalid_reason if concept else None,
            'classes': [
                {'code': cls.therapy_class.code, 'title': cls.therapy_class.title,
                 'concept_id': cls.therapy_class.concept_id}
                for cls in component.component_classes.all()
            ],
        })
    return {
        'code': regimen.code,
        'title': regimen.title,
        'concept_id': regimen.concept_id,
        'components': components,
    }
