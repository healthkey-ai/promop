"""The therapy-line write payload, packaged so a host can serve the editor.

Moved out of ``patient_portal``: the validation is clinical, not a property of the
in-place product, and an embedder that mounts the editor needs exactly it. The
in-place serializer imports these names, so both surfaces validate identically.
"""
from datetime import date

from rest_framework import serializers

from omop_core.models import Observation, PatientRecord, Person


class TherapyLineDrugSerializer(serializers.Serializer):
    """One drug given in a line, named by concept."""

    # Existing administrations are addressed by their own row, so repeated doses
    # of one drug survive a save (#1926).
    drug_exposure_id = serializers.IntegerField(required=False, min_value=1)
    concept_id = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    source_value = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=50,
    )

    def validate_start_date(self, value):
        if value > date.today():
            raise serializers.ValidationError(
                'A future drug administration is planned, not received treatment.')
        return value


class TherapyLineWriteSerializer(serializers.Serializer):
    """A line of therapy as a clinician describes it.

    Deliberately not a ModelSerializer: a line of therapy is not a row. It is a
    set of DrugExposures grouped by an Episode through EpisodeEvent, and the
    therapy fields on ``PatientRecord`` are inferred back out of that grouping.
    This is the vocabulary of the clinic -- which line, which drugs, which dates
    -- and the service turns it into the CDM shape.
    """

    person = serializers.PrimaryKeyRelatedField(queryset=Person.objects.all())
    line_number = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)
    drugs = TherapyLineDrugSerializer(many=True, required=False)
    regimen_concept_id = serializers.IntegerField(required=False, allow_null=True)
    outcome = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    intent = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=50)
    discontinuation_reason = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=60)
    source_value = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=50)
    # The cancer this line treats, as a disease name or slug. Omitted, the line
    # is the patient's primary cancer's (every client before #1739).
    disease = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)

    def validate_start_date(self, value):
        if value and value > date.today():
            raise serializers.ValidationError('start_date cannot be in the future.')
        return value

    def validate(self, attrs):
        start, end = attrs.get('start_date'), attrs.get('end_date')
        if start and end and end < start:
            raise serializers.ValidationError(
                {'end_date': 'end_date cannot precede start_date.'})
        # A line with neither drugs nor a named regimen groups nothing, so
        # inference reads it back as an empty line (#1924 adds a supported
        # assertion path for that; until then the editor must send one).
        if not self.partial and not attrs.get('drugs') and not attrs.get('regimen_concept_id'):
            raise serializers.ValidationError(
                'Provide at least one drug, or a regimen_concept_id naming the '
                'regimen. A line with neither groups no drug exposures and no '
                'therapy field would follow from it.',
            )
        if attrs.get('outcome'):
            from omop_core.services.treatment_catalog import outcomes_for_disease
            record = PatientRecord.objects.filter(person=attrs['person']).first()
            options = outcomes_for_disease(record.disease if record else '')
            aliases = {
                key: item['value'] for item in options
                for key in (item['code'], item['value'], item['label'])
            }
            value = attrs['outcome']
            if value not in aliases:
                # Preserve legacy values on a no-op edit; new selections must
                # belong to the patient's disease-specific catalog.
                if not Observation.objects.filter(
                    person=attrs['person'],
                    observation_source_value=f"LOT-{attrs['line_number']}-outcome",
                    value_as_string=value, is_erroneous=False,
                ).exists():
                    raise serializers.ValidationError(
                        {'outcome': 'Select an outcome for this disease.'})
            else:
                attrs['outcome'] = aliases[value]
        return attrs
