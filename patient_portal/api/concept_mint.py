"""Curator minting with a required, signed candidate-review step."""
from datetime import date
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.timezone import localdate
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary
from omop_core.mapping.suggestions import lexical_candidates, umls_candidates
from omop_core.services.pk import next_pk

# How many existing concepts the curator is shown before they may mint a new
# one. Minting is a curator decision, so this is the mint flow's own policy and
# not the suggest pipeline's -- `lexical_candidates` is shared with it only as a
# trigram search, and its default is tuned for what a ranking model reads in one
# prompt (ten). A person deciding whether a concept already exists is doing a
# different job: a match sitting at rank 11-25 that they never saw becomes a
# duplicate concept in the vocabulary, permanently. The direct name/code lookup
# below already uses 25; this keeps the two halves of one review the same width.
REVIEW_CANDIDATES = 25


class MintInput(serializers.Serializer):
    vocabulary_id = serializers.CharField(max_length=20)
    concept_name = serializers.CharField(min_length=3, max_length=255)
    concept_code = serializers.CharField(max_length=50)
    domain_id = serializers.CharField(max_length=20)
    source_code = serializers.CharField(max_length=255, allow_blank=True, default='')
    source_vocabulary_id = serializers.CharField(max_length=50, allow_blank=True, default='')

    def validate_vocabulary_id(self, value):
        if not value.startswith('HK-') or not Vocabulary.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Choose an existing HK-* vocabulary.')
        return value

    def validate_domain_id(self, value):
        if not Domain.objects.filter(pk=value).exists():
            raise serializers.ValidationError('Choose an existing domain.')
        return value


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mint_destination(request):
    from .views import _can_manage_field_mappings, _concept_name_search_filter, _serialize_concept

    if not _can_manage_field_mappings(request.user):
        return Response({'detail': 'Mapping curator access required.'}, status=403)
    inputs = MintInput(data=request.data)
    inputs.is_valid(raise_exception=True)
    data = inputs.validated_data
    action = request.data.get('action')
    evidence = {'user': str(request.user.pk), 'input': data}
    if action == 'review':
        # Run each retrieval path: this is a candidate review, not an automatic
        # ranker decision. Failures propagate, so an outage cannot authorize minting.
        umls, _ = umls_candidates(data['source_code'], data['source_vocabulary_id'], data['domain_id'])
        lexical = lexical_candidates(data['concept_name'], data['domain_id'],
                                     limit=REVIEW_CANDIDATES)
        ids = list(dict.fromkeys(c['concept_id'] for c in umls + lexical))
        direct = Concept.objects.filter(
            _concept_name_search_filter(data['concept_name']) | Q(concept_code=data['concept_code']),
            domain_id=data['domain_id'], invalid_reason__isnull=True,
            valid_start_date__lte=localdate(), valid_end_date__gte=localdate(),
        ).order_by('concept_id')[:25]
        ids = list(dict.fromkeys(ids + [c.pk for c in direct]))
        candidates = Concept.objects.filter(
            pk__in=ids, invalid_reason__isnull=True,
            valid_start_date__lte=localdate(), valid_end_date__gte=localdate(),
        )
        by_id = {c.pk: c for c in candidates}
        return Response({
            'candidates': [_serialize_concept(by_id[pk]) for pk in ids if pk in by_id],
            'review_token': signing.dumps(evidence, salt='mint-destination'),
        })
    if action != 'mint':
        return Response({'detail': 'Choose review or mint.'}, status=400)
    try:
        reviewed = signing.loads(request.data.get('review_token', ''), salt='mint-destination', max_age=900)
    except (signing.BadSignature, TypeError):
        return Response({'detail': 'Review candidate destinations again before minting.'}, status=400)
    if reviewed != evidence or request.data.get('none_match') is not True:
        return Response({'detail': 'Confirm that none of the reviewed candidates match.'}, status=400)
    try:
        with transaction.atomic():
            vocabulary = Vocabulary.objects.select_for_update().get(pk=data['vocabulary_id'])
            concept_class, _ = ConceptClass.objects.get_or_create(
                pk='Undefined', defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
            )
            concept = Concept.objects.create(
                concept_id=next_pk(Concept, 'concept_id'), vocabulary=vocabulary,
                concept_name=data['concept_name'], concept_code=data['concept_code'],
                domain_id=data['domain_id'], concept_class=concept_class,
                standard_concept=None, source='HealthKey', valid_start_date=localdate(),
                valid_end_date=date(2099, 12, 31),
            )
    except IntegrityError:
        return Response({'detail': 'That code already exists in this vocabulary. Choose the existing concept or another code.'}, status=409)
    return Response(_serialize_concept(concept), status=status.HTTP_201_CREATED)
