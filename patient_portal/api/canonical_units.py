"""Instance administration of canonical LOINC measurement units (v1 only)."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import Concept, LoincCodeClass, CanonicalUnitPreference, CanonicalUnitChange
from omop_core.services.canonical_units import UNIT_GROUPS, property_for


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def canonical_unit(request, concept_id):
    # Lock the stable concept row too: two first-time writes must serialize.
    concepts = Concept.objects.all()
    if request.method == 'PUT':
        if not request.user.is_staff:
            return Response({'detail': 'Instance administrator access required.'}, status=403)
        concepts = concepts.select_for_update()
    concept = get_object_or_404(concepts, pk=concept_id, vocabulary_id='LOINC', domain_id='Measurement')
    metadata = LoincCodeClass.objects.filter(pk=concept.concept_code).first()
    prop = property_for(concept, metadata)
    available = list(UNIT_GROUPS.get(prop, {}))
    preference = CanonicalUnitPreference.objects.filter(concept=concept).first()
    revision = preference.revision if preference else 0
    if request.method == 'PUT':
        if type(request.data.get('revision')) is not int or request.data['revision'] != revision:
            return Response({'detail': 'This unit setting changed. Reload it before saving.'}, status=409)
        unit = request.data.get('unit')
        if not isinstance(unit, str) or (unit and unit not in available):
            return Response({'detail': 'Choose a supported unit compatible with this LOINC property.'}, status=400)
        if unit and (concept.standard_concept != 'S' or concept.invalid_reason):
            return Response({'detail': 'Canonical units require an active standard measurement concept.'}, status=400)
        previous = preference.unit if preference else ''
        if previous != unit:
            preference, _ = CanonicalUnitPreference.objects.update_or_create(
                concept=concept, defaults={'unit': unit, 'property': prop, 'revision': revision + 1,
                                          'updated_by': request.user})
            CanonicalUnitChange.objects.create(preference=preference, previous_unit=previous,
                                              unit=unit, revision=preference.revision, changed_by=request.user)
    return Response({
        'concept_id': concept.pk, 'loinc_code': concept.concept_code,
        'unit': preference.unit if preference else '',
        'revision': preference.revision if preference else 0,
        'property': prop, 'available_units': available,
        'example_units': list(dict.fromkeys(u.strip() for u in (metadata.example_units if metadata else '').split(';') if u.strip())),
        'can_edit': bool(request.user.is_staff),
    })
