"""Computed subforms must show the reviewed labels and units (MIPI, #4559).

The native tabs already render "LDH (U/L)" and "WBC count (10³/µL)"; the shared
descriptor used to fall back to the model's underscore name, so a computed
subform read "Ldh u l" / "Wbc count thousand per ul" and carried no unit.
"""
import pytest

from omop_core.services.patient_form import patient_form_descriptors
from omop_core.services.write_descriptor import build_writable_field_descriptor

pytestmark = pytest.mark.django_db


def test_mipi_inputs_carry_reviewed_labels_and_units():
    descriptors = patient_form_descriptors(build_writable_field_descriptor())

    ldh = descriptors['ldh_u_l']
    assert ldh['label'] == 'LDH'
    assert ldh['unit'] == 'U/L'

    uln = descriptors['ldh_upper_limit_normal']
    assert uln['label'] == 'LDH upper limit of normal'
    assert uln['unit'] == 'U/L'

    wbc = descriptors['wbc_count_thousand_per_ul']
    assert wbc['label'] == 'WBC count'
    assert wbc['unit'] == '10³/µL'

    assert descriptors['mipi_risk']['label'] == 'MIPI Risk'
    assert descriptors['mipi_c_risk']['label'] == 'MIPI-C Risk'
    assert descriptors['ecog_performance_status']['label'] == 'ECOG performance status'
    assert descriptors['ki67_proliferation_index']['unit'] == '%'
