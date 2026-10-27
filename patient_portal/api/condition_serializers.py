"""V1 clinical facts may explicitly name the diagnosis they describe."""
from rest_framework import serializers

from omop_core.models import ConditionOccurrence
from omop_core.services.cancer_details import EVENT_FIELDS, condition_field_concepts
from .serializers import MeasurementSerializer, ObservationSerializer


class ConditionFactMixin:
    def _field_ids(self):
        if 'condition_field_concepts' not in self.context:
            self.context['condition_field_concepts'] = list(condition_field_concepts().values_list('pk', flat=True))
        return self.context['condition_field_concepts']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        event, kind = EVENT_FIELDS[self.Meta.model]
        supplied = 'condition_occurrence' in attrs
        # An owned bad assertion must remain retireable after its diagnosis
        # was itself retired/deleted. Re-activation still validates the link.
        if (not supplied and self.instance is not None and attrs.get('is_erroneous') is True
                and ('person' not in attrs or attrs['person'].pk == self.instance.person_id)):
            return attrs
        linked = (self.instance is not None and getattr(self.instance, event) is not None
                  and getattr(self.instance, kind) in self._field_ids())
        if not supplied and not linked:
            return attrs
        person = attrs.get('person') or (self.instance.person if self.instance else None)
        condition_id = attrs.pop('condition_occurrence', None)
        if not supplied and linked:
            condition_id = getattr(self.instance, event)
        if condition_id is not None:
            if (supplied and self.instance is not None and getattr(self.instance, event) is not None
                    and getattr(self.instance, kind) not in self._field_ids()):
                raise serializers.ValidationError({'condition_occurrence': 'This fact belongs to another OMOP event. Link its parent report to the diagnosis instead.'})
            cache = self.context.setdefault('condition_fact_parents', {})
            key = (person.pk if person else None, condition_id)
            if key not in cache:
                cache[key] = ConditionOccurrence.objects.filter(
                    pk=condition_id, person=person, is_erroneous=False).exists() if person else False
            if not cache[key]:
                raise serializers.ValidationError({'condition_occurrence': 'Must be a non-erroneous diagnosis belonging to this person.'})
            fields = self._field_ids()
            if not fields:
                raise serializers.ValidationError({'condition_occurrence': 'Load the CDM condition field vocabulary before linking a diagnosis.'})
            if supplied:
                attrs[event], attrs[kind] = condition_id, fields[0]
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        event, kind = EVENT_FIELDS[self.Meta.model]
        if getattr(instance, event) is not None and getattr(instance, kind) in self._field_ids():
            data['condition_occurrence'] = getattr(instance, event)
        else:
            data.pop('condition_occurrence', None)
        return data


class V1MeasurementSerializer(ConditionFactMixin, MeasurementSerializer):
    condition_occurrence = serializers.IntegerField(required=False, min_value=1,
        help_text='Diagnosis this fact describes. Must belong to the same person. Omit for an unlinked fact. Correct a link by naming another diagnosis; retire an incorrect fact with is_erroneous.')

    class Meta(MeasurementSerializer.Meta):
        fields = MeasurementSerializer.Meta.fields + ['condition_occurrence']


class V1ObservationSerializer(ConditionFactMixin, ObservationSerializer):
    condition_occurrence = serializers.IntegerField(required=False, min_value=1,
        help_text='Diagnosis this fact describes. Must belong to the same person. Omit for an unlinked fact. Correct a link by naming another diagnosis; retire an incorrect fact with is_erroneous.')

    class Meta(ObservationSerializer.Meta):
        fields = ObservationSerializer.Meta.fields + ['condition_occurrence']
