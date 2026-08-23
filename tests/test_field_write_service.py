"""Descriptor-driven field write applier (CB #2omop-federated-UI Phase 2 / A2).

apply_field_writes turns {projection_field: value} into the OMOP fact the descriptor names
(KIND_EDITABLE), buckets KIND_PROFILE for the persons endpoint, and rejects the rest with the
descriptor's own reason. These tests anchor it against the SAME derivation the read path uses:
a written fact must re-derive back to the value on PatientRecord.
"""
from datetime import date

import pytest

from omop_core.models import Location, Measurement
from omop_core.services.field_write_service import apply_field_writes, owned_writable_fields
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _person_with_loinc(code, name):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code=code, concept_name=name)  # LOINC by factory default
    _seed_patient_reported_type()
    return person


def _seed_patient_reported_type():
    # Patient-authored facts carry the 'Patient self-report' type (32865) in OMOP's 'Type Concept'
    # vocabulary; the applier fails closed without it (and rejects a look-alike in another vocab).
    tc = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=32865, concept_code='PT-SELF-REPORT',
                   concept_name='Patient self-report', vocabulary=tc)


def test_owned_writable_fields_are_what_the_applier_persists():
    # The editor gates on this set, so it must include the labs + demographic/location this applier
    # writes and EXCLUDE the descriptor-writable-but-deferred identity fields (email/phone).
    from omop_core.services.write_descriptor import build_writable_field_descriptor, KIND_PROFILE
    ConceptFactory(concept_code='718-7', concept_name='Hemoglobin')  # make a LOINC field resolvable
    owned = owned_writable_fields()
    assert {'gender', 'race', 'ethnicity', 'city', 'region'} <= owned
    # email is _PROFILE_REPLACEABLE — descriptor-writable but bucketed by the applier → not owned.
    desc = build_writable_field_descriptor()
    if desc.get('email', {}).get('kind') == KIND_PROFILE and desc['email'].get('writable'):
        assert 'email' not in owned


@pytest.mark.django_db
def test_editable_lab_writes_a_measurement_and_rederives():
    # hemoglobin_g_dl is a KIND_EDITABLE LOINC (718-7) measurement. Writing it must create one
    # Measurement fact AND re-derive the PatientRecord back to the same value (write anchored to read).
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'hemoglobin_g_dl': 12.5}, today=date(2026, 8, 23))

    assert result.applied == ['hemoglobin_g_dl']
    assert not result.rejected and not result.profile
    assert Measurement.objects.filter(person=person).count() == 1
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(12.5)


def test_same_day_re_edit_upserts_not_duplicates():
    # A same-day correction updates the existing dated fact rather than appending a second row.
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    apply_field_writes(person, {'hemoglobin_g_dl': 12.5}, today=date(2026, 8, 23))
    apply_field_writes(person, {'hemoglobin_g_dl': 13.0}, today=date(2026, 8, 23))

    assert Measurement.objects.filter(person=person).count() == 1
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(13.0)


def test_demographic_write_sets_person_and_is_applied():
    # gender is KIND_PROFILE demographic — written on Person (concept + source_value), not a fact.
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'gender': 'Female'}, today=date(2026, 8, 23))

    assert 'gender' in result.applied
    assert not result.profile and not result.rejected
    assert Measurement.objects.filter(person=person).count() == 0
    person.refresh_from_db()
    assert person.gender_source_value == 'Female'  # raw text kept regardless of concept resolution


def test_location_write_upserts_and_links_a_location_row():
    # city/region are KIND_PROFILE location — one PATCH upserts ONE Location and links it by id.
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'city': 'Boston', 'region': 'MA'}, today=date(2026, 8, 23))

    assert set(result.applied) == {'city', 'region'}
    person.refresh_from_db()
    assert person.location_id is not None
    loc = Location.objects.get(location_id=person.location_id)
    assert (loc.city, loc.state) == ('Boston', 'MA')  # region → OMOP Location.state


def test_replaceable_identity_field_is_bucketed_not_written():
    # An identity/admin KIND_PROFILE field this applier does not own (email/phone/validated/…) is
    # handed back in `profile`, never silently written here. Pick one from the live descriptor so the
    # test tracks the real contract rather than a guessed field name.
    from omop_core.services.write_descriptor import build_writable_field_descriptor, KIND_PROFILE
    desc = build_writable_field_descriptor()
    owned = set(('gender', 'race', 'ethnicity', 'city', 'region', 'postal_code', 'country',
                 'latitude', 'longitude'))
    replaceable = [f for f, e in desc.items()
                   if e.get('kind') == KIND_PROFILE and e.get('writable') and f not in owned]
    if not replaceable:
        pytest.skip('no replaceable KIND_PROFILE field in this descriptor')
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {replaceable[0]: 'x@example.com'}, today=date(2026, 8, 23))

    assert replaceable[0] in result.profile
    assert result.applied == []


def test_computed_field_is_rejected_with_the_descriptor_reason():
    # bmi is KIND_COMPUTED (writable=False) — height×weight, never authored alone. Rejected, not
    # silently dropped, so the caller can tell the user exactly why.
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'bmi': 27.0}, today=date(2026, 8, 23))

    assert 'bmi' in result.rejected
    assert result.applied == []
    assert Measurement.objects.filter(person=person).count() == 0


def test_unknown_field_is_rejected():
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'not_a_real_field': 1}, today=date(2026, 8, 23))

    assert 'not_a_real_field' in result.rejected
    assert result.applied == []


@pytest.mark.parametrize('cleared', [None, '', '   '])
def test_clear_is_rejected_not_a_silent_clear_or_500(cleared):
    # None AND empty/whitespace strings (what the widget's inputs emit on clear) are a clear, not a
    # fact write (safe-delete with provenance is deferred, #4833). Rejected (reported), never
    # applied, never raised — a batch PATCH carrying an emptied field must not fail the whole request.
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    result = apply_field_writes(person, {'hemoglobin_g_dl': cleared}, today=date(2026, 8, 23))

    assert 'hemoglobin_g_dl' in result.rejected
    assert result.applied == []
    assert Measurement.objects.filter(person=person).count() == 0


def test_out_of_range_value_is_rejected_not_a_db_overflow():
    # value_as_number is DecimalField(15,5): a >10-integer-digit value must raise (→ 400 at the
    # caller), not reach Postgres as a numeric overflow (500).
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    with pytest.raises(ValueError):
        apply_field_writes(person, {'hemoglobin_g_dl': 10 ** 12}, today=date(2026, 8, 23))


def test_missing_type_concept_fails_closed():
    # No 'Patient self-report' type (32865) loaded → the fact would get an OMOP-invalid type concept,
    # so the applier must fail closed rather than write a corrupt type dimension.
    from omop_core.models import Concept
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code='718-7', concept_name='Hemoglobin')  # LOINC present, type absent
    assert not Concept.objects.filter(concept_id=32865).exists()

    with pytest.raises(ValueError):
        apply_field_writes(person, {'hemoglobin_g_dl': 12.5}, today=date(2026, 8, 23))


def test_same_day_imported_fact_is_not_overwritten():
    # Provenance: a same-day IMPORTED Lab fact (type=32856) for the same concept must NOT be touched —
    # the applier writes a SEPARATE patient-self-report (32865) row. This is the whole point of scoping
    # the upsert by type.
    from datetime import date as _date
    from omop_core.models import Concept, Measurement
    from tests.factories import MeasurementFactory
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab_type = ConceptFactory(concept_id=32856, concept_code='LAB', concept_name='Lab result')
    hgb = Concept.objects.get(concept_code='718-7')
    imported = MeasurementFactory(
        person=person, measurement_concept=hgb, measurement_type_concept=lab_type,
        measurement_date=_date(2026, 8, 23), value_as_number=9.9, measurement_source_value='FHIR')

    apply_field_writes(person, {'hemoglobin_g_dl': 12.5}, today=_date(2026, 8, 23))

    imported.refresh_from_db()
    assert float(imported.value_as_number) == 9.9                    # import untouched
    assert imported.measurement_source_value == 'FHIR'
    ours = Measurement.objects.filter(
        person=person, measurement_concept=hgb, measurement_type_concept_id=32865)
    assert ours.count() == 1 and float(ours.first().value_as_number) == 12.5   # our separate row
    assert Measurement.objects.filter(person=person, measurement_concept=hgb).count() == 2


def test_provenance_concept_in_the_wrong_vocabulary_fails_closed():
    # 32865 exists but in a shadow vocab (not OMOP 'Type Concept') → do not stamp facts with a
    # look-alike provenance concept; fail closed.
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code='718-7', concept_name='Hemoglobin')
    ConceptFactory(concept_id=32865, concept_code='PT-SELF-REPORT', concept_name='Patient self-report')  # default LOINC vocab

    with pytest.raises(ValueError):
        apply_field_writes(person, {'hemoglobin_g_dl': 12.5}, today=date(2026, 8, 23))


def test_overlong_and_out_of_range_location_values_are_rejected():
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    with pytest.raises(ValueError):
        apply_field_writes(person, {'region': 'Massachusetts'}, today=date(2026, 8, 23))  # state is varchar(2)
    with pytest.raises(ValueError):
        apply_field_writes(person, {'latitude': 1000}, today=date(2026, 8, 23))  # ±90
    with pytest.raises(ValueError):
        apply_field_writes(person, {'latitude': 'NaN'}, today=date(2026, 8, 23))  # non-finite


def test_location_edit_clones_a_shared_row_instead_of_corrupting_co_located_patients():
    # A Location deduplicated across patients must not be edited in place — clone + relink only this one.
    person_a = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    person_b = PersonFactory()
    PatientRecordFactory(person=person_b)
    shared = Location.objects.create(
        location_id=990001, city='Boston', state='MA', address_1='1 Main St', county='Suffolk')
    for p in (person_a, person_b):
        p.location_id = shared.location_id
        p.save(update_fields=['location_id'])

    apply_field_writes(person_a, {'city': 'Cambridge'}, today=date(2026, 8, 23))

    person_a.refresh_from_db(); person_b.refresh_from_db()
    assert person_a.location_id != shared.location_id                 # A got its own copy
    assert person_b.location_id == shared.location_id                 # B still points at the shared row
    assert Location.objects.get(location_id=shared.location_id).city == 'Boston'   # B's address intact
    a_loc = Location.objects.get(location_id=person_a.location_id)
    assert a_loc.city == 'Cambridge'
    assert a_loc.state == 'MA'                          # projected column kept
    assert (a_loc.address_1, a_loc.county) == ('1 Main St', 'Suffolk')  # NON-projected columns kept too


def test_lymph_node_size_writes_the_disambiguating_qualifier():
    # largest_lymph_node_size shares LOINC 21889-1 with tumor size; the fact must carry
    # qualifier_source_value='lymph-node' or _get_cll_data won't read it back (round-trip breaks).
    from omop_core.models import Measurement
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code='21889-1', concept_name='Size Tumor')
    _seed_patient_reported_type()

    result = apply_field_writes(person, {'largest_lymph_node_size': 25}, today=date(2026, 8, 23))

    assert 'largest_lymph_node_size' in result.applied
    m = Measurement.objects.get(person=person, measurement_concept__concept_code='21889-1')
    assert m.qualifier_source_value == 'lymph-node'
    assert float(m.value_as_number) == 25


def test_non_numeric_value_for_a_number_field_fails_closed():
    person = _person_with_loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')

    with pytest.raises(ValueError):
        apply_field_writes(person, {'hemoglobin_g_dl': 'high'}, today=date(2026, 8, 23))


# --- genetic_mutations: the LIST-diff write path ---------------------------------------------------
# Unlike a scalar lab, genetic_mutations is a list — one Measurement per gene (gene LOINC concept,
# variant→value_as_string, origin→qualifier_concept, interpretation→value_as_concept). Genes dropped
# from a later PATCH are marked entered-in-error (a safe delete of OUR 32865-typed rows). Every test
# anchors the write to the SAME derivation the read path uses.
from omop_core.services.patient_record_service import _GENETIC_MUTATION_LOINCS  # noqa: E402

_GENE_CODE = {g: c for c, g in _GENETIC_MUTATION_LOINCS.items()}
_MUT_SNOMED_IDS = (255395001, 255461003, 30166007, 10828004, 42425007)  # germline/somatic + path/benign/vus


def _seed_genetic_vocab(genes=('BRCA1', 'BRCA2', 'TP53')):
    for g in genes:
        ConceptFactory(concept_code=_GENE_CODE[g], concept_name=f'{g} gene mutation analysis')  # LOINC
    _seed_patient_reported_type()
    for cid in _MUT_SNOMED_IDS:  # the applier/derivation key origin & interpretation by concept_id
        ConceptFactory(concept_id=cid, concept_code=str(cid), concept_name=f'SNOMED {cid}')


def _person_with_genetic_vocab(genes=('BRCA1', 'BRCA2', 'TP53')):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    _seed_genetic_vocab(genes)
    return person


def test_genetic_mutation_write_creates_measurement_and_rederives():
    person = _person_with_genetic_vocab()
    muts = [{'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline',
             'interpretation': 'pathogenic'}]

    result = apply_field_writes(person, {'genetic_mutations': muts}, today=date(2026, 8, 23))

    assert result.applied == ['genetic_mutations']
    m = Measurement.objects.get(person=person, measurement_concept__concept_code=_GENE_CODE['BRCA1'])
    assert m.value_as_string == 'c.68_69delAG'
    assert m.measurement_type_concept_id == 32865
    assert m.qualifier_concept_id == 255395001            # germline
    assert m.value_as_concept_id == 30166007              # pathogenic
    assert refresh_patient_record(person).genetic_mutations == [
        {'gene': 'brca1', 'variant': 'c.68_69delAG', 'test_date': '2026-08-23',
         'origin': 'germline', 'interpretation': 'pathogenic'}]


def test_genetic_mutation_same_gene_upserts_not_duplicates():
    # A correction to the SAME gene updates its one row (keyed by gene, not by date), never appends.
    person = _person_with_genetic_vocab()
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': '185delAG', 'origin': 'somatic', 'interpretation': 'vus'}]},
        today=date(2026, 8, 24))

    active = Measurement.objects.filter(
        person=person, measurement_concept__concept_code=_GENE_CODE['BRCA1'], is_erroneous=False)
    assert active.count() == 1
    assert refresh_patient_record(person).genetic_mutations == [
        {'gene': 'brca1', 'variant': '185delAG', 'test_date': '2026-08-24',
         'origin': 'somatic', 'interpretation': 'vus'}]


def test_genetic_mutation_list_diff_removes_dropped_gene():
    # A gene present before but absent from a later PATCH is marked entered-in-error (dropped from
    # the derivation); genes still sent are kept. This is the list-diff reconcile.
    person = _person_with_genetic_vocab()
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'},
        {'gene': 'TP53', 'mutation': 'R175H', 'origin': 'somatic', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 24))

    tp53 = Measurement.objects.get(person=person, measurement_concept__concept_code=_GENE_CODE['TP53'])
    assert tp53.is_erroneous is True
    assert {m['gene'] for m in refresh_patient_record(person).genetic_mutations} == {'brca1'}


def test_genetic_mutation_empty_list_clears_all():
    person = _person_with_genetic_vocab()
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))

    result = apply_field_writes(person, {'genetic_mutations': []}, today=date(2026, 8, 24))

    assert 'genetic_mutations' in result.applied
    assert refresh_patient_record(person).genetic_mutations == []
    assert not Measurement.objects.filter(
        person=person, measurement_type_concept_id=32865, is_erroneous=False).exists()


def test_genetic_mutation_reconcile_does_not_touch_imported_row():
    # Removal only ever flags OUR patient-reported (32865) mutation rows — an imported (Lab-typed)
    # mutation for the same gene is never marked erroneous.
    from tests.factories import MeasurementFactory
    from omop_core.models import Concept
    person = _person_with_genetic_vocab()
    lab_type = ConceptFactory(concept_id=32856, concept_code='LAB', concept_name='Lab result')
    brca1 = Concept.objects.get(concept_code=_GENE_CODE['BRCA1'], vocabulary_id='LOINC')
    imported = MeasurementFactory(
        person=person, measurement_concept=brca1, measurement_type_concept=lab_type,
        measurement_date=date(2026, 8, 23), value_as_string='IMPORTED-VARIANT')

    apply_field_writes(person, {'genetic_mutations': []}, today=date(2026, 8, 24))

    imported.refresh_from_db()
    assert imported.is_erroneous is False
    assert imported.value_as_string == 'IMPORTED-VARIANT'


def test_genetic_mutation_optional_origin_and_interpretation_omitted():
    # origin/interpretation are optional: a gene+variant with neither round-trips (the derivation just
    # omits those keys), and the write does not fail.
    person = _person_with_genetic_vocab()

    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG'}]}, today=date(2026, 8, 23))

    assert refresh_patient_record(person).genetic_mutations == [
        {'gene': 'brca1', 'variant': 'c.68_69delAG', 'test_date': '2026-08-23'}]


def test_genetic_mutation_recognized_gene_without_variant_fails_closed():
    # A gene with no variant would write value_as_string=None, which the derivation skips — the mutation
    # would silently vanish. Reject it instead so the caller gets a 400, never a silent drop.
    person = _person_with_genetic_vocab()

    with pytest.raises(ValueError):
        apply_field_writes(person, {'genetic_mutations': [
            {'gene': 'BRCA1', 'mutation': '', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
            today=date(2026, 8, 23))


def test_genetic_mutation_supported_but_unloaded_snomed_fails_closed():
    # origin='germline' is supported, but on a stack where the SNOMED concept is not loaded the write
    # would silently drop the origin (breaking round-trip). Fail closed instead of accepting a lossy edit.
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code=_GENE_CODE['BRCA1'], concept_name='BRCA1 gene')  # gene LOINC + type only
    _seed_patient_reported_type()

    with pytest.raises(ValueError):
        apply_field_writes(person, {'genetic_mutations': [
            {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline'}]}, today=date(2026, 8, 23))


def test_genetic_mutation_all_unknown_genes_fails_closed_not_a_wipe():
    # A non-empty list that resolves to NOTHING (all unknown genes / junk) must fail closed, not be read
    # as "remove everything" — otherwise a malformed PATCH silently wipes the saved mutation history.
    person = _person_with_genetic_vocab()
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))

    with pytest.raises(ValueError):
        apply_field_writes(
            person, {'genetic_mutations': [{'gene': 'NOTAGENE', 'mutation': 'x'}]}, today=date(2026, 8, 24))

    # The raise rolls the caller's transaction back; here (no atomic) assert the existing row survived.
    assert Measurement.objects.filter(
        person=person, measurement_concept__concept_code=_GENE_CODE['BRCA1'], is_erroneous=False).exists()


def test_genetic_mutation_mixed_known_and_unknown_writes_known_and_reconciles():
    # A list mixing a known gene with an unknown one writes the known gene, skips the unknown, and
    # reconciles normally (written_codes is non-empty, so no fail-closed and no over-wipe).
    person = _person_with_genetic_vocab()
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'},
        {'gene': 'TP53', 'mutation': 'R175H', 'origin': 'somatic', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))

    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'},
        {'gene': 'NOTAGENE', 'mutation': 'x'}]},
        today=date(2026, 8, 24))

    # BRCA1 kept, TP53 (omitted) retired, NOTAGENE never written.
    assert {m['gene'] for m in refresh_patient_record(person).genetic_mutations} == {'brca1'}


def test_genetic_mutation_does_not_touch_imported_self_report_gene_fact():
    # The sharpest provenance case: an IMPORTED gene fact that legitimately carries the shared
    # 'Patient self-report' type (32865) but NOT our ownership sentinel — e.g. a FHIR patient-reported
    # genomic result — must never be overwritten or entered-in-error'd by the widget's list-diff.
    from tests.factories import MeasurementFactory
    from omop_core.models import Concept
    person = _person_with_genetic_vocab()
    tc = Concept.objects.get(concept_id=32865)
    brca1 = Concept.objects.get(concept_code=_GENE_CODE['BRCA1'], vocabulary_id='LOINC')
    imported = MeasurementFactory(
        person=person, measurement_concept=brca1, measurement_type_concept=tc,
        measurement_date=date(2026, 8, 20), value_as_string='IMPORTED-VARIANT',
        measurement_source_value='FHIR', is_erroneous=False)

    # (a) Writing the SAME gene must not overwrite the import — it lands in our own sentinel row.
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 23))
    imported.refresh_from_db()
    assert imported.value_as_string == 'IMPORTED-VARIANT' and imported.is_erroneous is False

    # (b) A later clear must not retire the import either.
    apply_field_writes(person, {'genetic_mutations': []}, today=date(2026, 8, 24))
    imported.refresh_from_db()
    assert imported.is_erroneous is False


def test_genetic_mutation_reconcile_ignores_same_code_in_other_vocabulary():
    # concept_code is unique only within a vocabulary. A patient-reported fact that reuses a gene's
    # code in a NON-LOINC vocabulary must not be retired when that gene is dropped from the list.
    from tests.factories import MeasurementFactory
    from omop_core.models import Concept
    person = _person_with_genetic_vocab()
    other_vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    decoy = ConceptFactory(concept_code=_GENE_CODE['BRCA1'], concept_name='not a gene', vocabulary=other_vocab)
    tc = Concept.objects.get(concept_id=32865)
    decoy_row = MeasurementFactory(
        person=person, measurement_concept=decoy, measurement_type_concept=tc,
        measurement_date=date(2026, 8, 23), value_as_string='UNRELATED', is_erroneous=False)

    # Submit a DIFFERENT gene only → BRCA1 code is "dropped", but the decoy is non-LOINC and must survive.
    apply_field_writes(person, {'genetic_mutations': [
        {'gene': 'TP53', 'mutation': 'R175H', 'origin': 'somatic', 'interpretation': 'pathogenic'}]},
        today=date(2026, 8, 24))

    decoy_row.refresh_from_db()
    assert decoy_row.is_erroneous is False
    assert decoy_row.value_as_string == 'UNRELATED'


def test_genetic_mutation_non_list_is_rejected():
    person = _person_with_genetic_vocab()

    result = apply_field_writes(person, {'genetic_mutations': 'BRCA1'}, today=date(2026, 8, 23))

    assert 'genetic_mutations' in result.rejected
    assert result.applied == []


def test_owned_writable_includes_genetic_mutations_only_when_vocab_loaded():
    # Mirrors KIND_EDITABLE's "writable iff concept resolves": genetic_mutations is offered only when
    # its vocab (patient-report type + a gene LOINC) is loaded, so the editor never dangles an input
    # whose write would no-op on a partial-vocab stack.
    assert 'genetic_mutations' not in owned_writable_fields()
    _seed_genetic_vocab(('BRCA1',))
    assert 'genetic_mutations' in owned_writable_fields()
