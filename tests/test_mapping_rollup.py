"""Grouping the review queue by label instead of by vendor code."""
import pytest

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.mapping_rollup import group_entries, group_members

pytestmark = pytest.mark.django_db


def row(code, description='', seen=0, **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, source_code_description=description, occurrence_count=seen,
        **{'source_vocabulary_id': 'EPIC', 'status': 'proposed', **kwargs})


def entries(page=1, page_size=100):
    return group_entries(SourceCodeConceptMapping.objects.all(), page, page_size)


def test_codes_sharing_a_label_become_one_entry_carrying_their_summed_seen():
    """The point of the whole thing: albumin arrives under 2,557 codes, and its
    largest single row carries 1.6% of the label's real weight."""
    for i, spelling in enumerate(['Albumin', 'ALBUMIN', 'albumin ']):
        row(f'EPIC#{i}', spelling, seen=100)
    found, total = entries()
    assert total == 1
    assert found[0]['label'] == 'albumin'
    assert found[0]['members'] == 3
    assert found[0]['seen'] == 300
    # No single row stands for the group, so there is no id to act on.
    assert found[0]['mapping_id'] is None


def test_a_lone_code_is_an_entry_that_still_names_its_row():
    row('SOLO', 'Ferritin', seen=7)
    found, _ = entries()
    assert found[0]['members'] == 1
    assert found[0]['mapping_id'] == SourceCodeConceptMapping.objects.get(source_code='SOLO').pk


def test_entries_are_ordered_by_summed_seen_not_by_the_largest_member():
    """A per-code queue puts the one big code first. Albumin's 3 codes of 100
    outrank it once they are added up -- which is the ordering bug."""
    row('BIG', 'Hourly Rounding Bundle', seen=250)
    for i in range(3):
        row(f'ALB{i}', 'Albumin', seen=100)
    found, _ = entries()
    assert [(e['label'], e['seen']) for e in found] == [('albumin', 300), ('hourlyroundingbundle', 250)]


def test_rows_with_no_label_stay_separate_instead_of_becoming_one_group():
    """1,929 staging rows carry no description. SQL groups NULLs, so without a
    synthetic key they would form a single entry standing for 1,929 unrelated
    codes -- mappable in one click."""
    for i, blank in enumerate(['', '   ', '--', '()']):
        row(f'BLANK{i}', blank, seen=5)
    found, total = entries()
    assert total == 4
    assert all(e['members'] == 1 and e['label'] is None for e in found)
    assert len({e['mapping_id'] for e in found}) == 4


def test_a_synthetic_key_cannot_collide_with_a_real_label():
    """':' is outside the [a-z0-9%#] set a real key is built from."""
    row('HASHY', '# Neutrophils', seen=1)
    row('NOLABEL', '', seen=1)
    found, _ = entries()
    labels = {e['label'] for e in found}
    assert '#neutrophils' in labels and None in labels


def test_a_group_reports_a_destination_only_when_every_member_agrees(concept_pair):
    one, two = concept_pair
    row('A', 'Albumin', seen=1, target_concept=one)
    row('B', 'ALBUMIN', seen=1, target_concept=one)
    found, _ = entries()
    assert found[0]['destination_concept_id'] == one.concept_id
    assert found[0]['destination_concept_name'] == one.concept_name
    assert found[0]['mixed_destinations'] is False

    row('C', 'albumin', seen=1, target_concept=two)
    found, _ = entries()
    assert found[0]['destination_concept_id'] is None
    # The name goes too: naming one side of a disagreement is worse than
    # naming neither.
    assert found[0]['destination_concept_name'] is None
    assert found[0]['mixed_destinations'] is True


def test_an_unanswered_member_is_a_disagreement_not_an_absence(concept_pair):
    """Count(distinct) skips NULL, so a group of "one concept + two blanks"
    would otherwise read as unanimous."""
    one, _ = concept_pair
    row('A', 'Albumin', seen=1, target_concept=one)
    row('B', 'ALBUMIN', seen=1)
    found, _ = entries()
    assert found[0]['mixed_destinations'] is True
    assert found[0]['destination_concept_id'] is None


def test_a_group_reports_how_many_members_a_write_may_touch():
    """An approved or rejected member is somebody's decision, not a gap."""
    row('A', 'Albumin', seen=1)
    row('B', 'ALBUMIN', seen=1, status='approved')
    row('C', 'albumin', seen=1, status='rejected')
    found, _ = entries()
    assert found[0]['members'] == 3
    assert found[0]['proposed'] == 1
    assert found[0]['mixed_statuses'] is True
    assert found[0]['status'] is None


def test_pagination_counts_groups_not_rows():
    for i in range(250):
        row(f'C{i:03}', f'Analyte {i}', seen=1000 - i)
    for i in range(50):
        row(f'DUP{i}', 'Albumin', seen=1)
    first, total = entries(page=1, page_size=100)
    assert total == 251
    assert len(first) == 100
    second, _ = entries(page=2, page_size=100)
    assert not {e['label'] for e in first} & {e['label'] for e in second}


def test_members_returns_the_rows_behind_an_entry():
    for i, spelling in enumerate(['Albumin', 'ALBUMIN', 'albumin ']):
        row(f'EPIC#{i}', spelling)
    row('OTHER', 'Ferritin')
    members = group_members(SourceCodeConceptMapping.objects.all(), 'albumin')
    assert sorted(members.values_list('source_code', flat=True)) == ['EPIC#0', 'EPIC#1', 'EPIC#2']


def test_members_of_a_synthetic_key_is_the_one_row():
    blank = row('BLANK', '')
    row('OTHER', '')
    members = group_members(SourceCodeConceptMapping.objects.all(), f':{blank.pk}')
    assert list(members.values_list('pk', flat=True)) == [blank.pk]


@pytest.mark.parametrize('bad', [':notanumber', ':', ':999999999'])
def test_a_malformed_or_unknown_key_returns_nothing_rather_than_raising(bad):
    row('A', 'Albumin')
    assert not group_members(SourceCodeConceptMapping.objects.all(), bad).exists()


@pytest.fixture
def concept_pair():
    from tests.factories import ConceptFactory
    return ConceptFactory(concept_id=1001), ConceptFactory(concept_id=1002)


# --- through the API --------------------------------------------------------

@pytest.fixture
def api():
    from rest_framework.test import APIRequestFactory, force_authenticate
    from patient_portal.api.views import code_mapping_group, code_mapping_list
    from patient_portal.models import Identity
    user = Identity.objects.create_user(email='rollup@example.test', is_staff=True)

    def call(view, path, **params):
        request = APIRequestFactory().get(path, params)
        force_authenticate(request, user=user)
        return view(request)
    return lambda **p: call(code_mapping_list, '/api/v1/code-mappings/', browse='1', **p), \
        lambda **p: call(code_mapping_group, '/api/v1/code-mappings/group/', **p)


def test_the_flat_queue_is_still_the_default(api):
    browse, _ = api
    for i in range(3):
        row(f'A{i}', 'Albumin', seen=10)
    flat = browse().data
    assert flat['rollup'] is False
    assert flat['groups'] == {}
    assert len(flat['results']) == 3
    assert flat['pages']['Unmapped']['total'] == 3


def test_rollup_returns_one_entry_per_label_and_pages_by_group(api):
    browse, _ = api
    for i in range(3):
        row(f'A{i}', 'Albumin', seen=10)
    row('F', 'Ferritin', seen=5)
    rolled = browse(rollup='1').data
    assert rolled['rollup'] is True
    unmapped = rolled['groups']['Unmapped']
    assert [(e['label'], e['members'], e['seen']) for e in unmapped] == [
        ('albumin', 3, 30), ('ferritin', 1, 5)]
    assert rolled['pages']['Unmapped']['total'] == 2


def test_expanding_an_entry_returns_its_codes_newest_volume_first(api):
    _, members = api
    row('LOW', 'Albumin', seen=1)
    row('HIGH', 'ALBUMIN', seen=99)
    row('ELSEWHERE', 'Ferritin', seen=50)
    data = members(label='albumin').data
    assert [r['source_code'] for r in data['results']] == ['HIGH', 'LOW']
    assert data['truncated'] is False


def test_expanding_is_scoped_to_the_section_the_entry_came_from(api):
    """A label split across sections is two entries; expanding one must not
    show the other's rows."""
    _, members = api
    row('PROP', 'Albumin', seen=1)
    row('DONE', 'ALBUMIN', seen=1, status='approved')
    assert [r['source_code'] for r in members(label='albumin', section='Unmapped').data['results']] == ['PROP']
    assert [r['source_code'] for r in members(label='albumin', section='Mapped').data['results']] == ['DONE']


def test_expanding_a_single_unlabelled_row_uses_its_synthetic_key(api):
    _, members = api
    blank = row('BLANK', '', seen=1)
    row('OTHER', '', seen=1)
    data = members(label=f':{blank.pk}').data
    assert [r['source_code'] for r in data['results']] == ['BLANK']


def test_a_very_large_group_is_cut_and_says_so(api, settings):
    from patient_portal.api import views
    _, members = api
    original = views.MAX_GROUP_MEMBERS
    views.MAX_GROUP_MEMBERS = 3
    try:
        for i in range(5):
            row(f'A{i}', 'Albumin', seen=i)
        data = members(label='albumin').data
        assert len(data['results']) == 3
        assert data['truncated'] is True
    finally:
        views.MAX_GROUP_MEMBERS = original


def test_the_members_endpoint_validates_its_inputs(api):
    _, members = api
    assert members().status_code == 400
    assert members(label='albumin', section='Nonsense').status_code == 400
