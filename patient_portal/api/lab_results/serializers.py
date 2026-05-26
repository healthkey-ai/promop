from rest_framework import serializers


class LabValueSerializer(serializers.Serializer):
    measurement_id = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=15, decimal_places=5, allow_null=True)
    value_string = serializers.CharField(allow_null=True)
    unit = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    measured_at = serializers.DateField()
    range_low = serializers.DecimalField(max_digits=15, decimal_places=5, allow_null=True)
    range_high = serializers.DecimalField(max_digits=15, decimal_places=5, allow_null=True)
    source = serializers.CharField(allow_null=True)
    lab_name = serializers.CharField(allow_null=True)
    report_filename = serializers.CharField(allow_null=True)


class MeasurementUpdateSerializer(serializers.Serializer):
    value = serializers.DecimalField(
        max_digits=15, decimal_places=5, required=False, allow_null=True,
    )
    value_string = serializers.CharField(
        max_length=60, required=False, allow_null=True, allow_blank=True,
    )
    measured_at = serializers.DateField(required=False)
    range_low = serializers.DecimalField(
        max_digits=15, decimal_places=5, required=False, allow_null=True,
    )
    range_high = serializers.DecimalField(
        max_digits=15, decimal_places=5, required=False, allow_null=True,
    )


class LabResultCardSerializer(serializers.Serializer):
    concept_id = serializers.IntegerField()
    concept_code = serializers.CharField()
    concept_name = serializers.CharField()
    original_name = serializers.CharField(allow_null=True)
    vocabulary_id = serializers.CharField()
    category = serializers.CharField()
    values = LabValueSerializer(many=True)
