"""
Personal-representative (proxy-authorization) review API — PHR-S FM PH.6.3#04
("render PHR authorization info: the designee(s) authorized to receive information").

Read-only. Staff/superusers and trusted service tokens see all grants; any other
authenticated user sees only grants where they are the representative or which cover
their own record (as the account holder). Managing/verifying grants is out of scope
here — this endpoint renders the authorization information.
"""
from django.db.models import Q
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from omop_core.models import PersonalRepresentative
from .permissions import is_service_token


class PersonalRepresentativeSerializer(serializers.ModelSerializer):
    representative_email = serializers.CharField(source='representative.email', read_only=True)
    representative_name = serializers.CharField(source='representative.name', read_only=True)

    class Meta:
        model = PersonalRepresentative
        fields = [
            'id', 'person_id', 'representative', 'representative_email',
            'representative_name', 'relationship', 'verification_status', 'granted_at',
        ]
        read_only_fields = fields


class PersonalRepresentativeViewSet(viewsets.ReadOnlyModelViewSet):
    """Render proxy-authorization grants (PH.6.3#04)."""
    serializer_class = PersonalRepresentativeSerializer
    permission_classes = [IsAuthenticated]

    def _is_privileged(self):
        request = self.request
        user = request.user
        return bool(
            is_service_token(request)
            or getattr(user, 'is_superuser', False)
            or getattr(user, 'is_staff', False)
        )

    def get_queryset(self):
        qs = PersonalRepresentative.objects.select_related('representative').all()
        if self._is_privileged():
            return qs
        user = self.request.user
        from patient_portal.services import patient_person_for
        cond = Q(representative=user)  # grants where the caller is the designee
        person = patient_person_for(user)
        if person is not None:
            cond |= Q(person_id=person.person_id)  # grants over the caller's own record
        return qs.filter(cond)
