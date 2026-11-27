"""The Wearables tab's field -> metric key map (a JSON file the frontend imports)
must be the server's `WEARABLE_METRIC_FIELDS`, so a 30-day value links to the
source codes that really feed it."""
import json
from pathlib import Path

from omop_core.services.mappings import WEARABLE_METRIC_FIELDS

PATH = (Path(__file__).resolve().parent.parent
        / 'frontend/src/components/PatientInfo/tabs/wearableMetricKeys.json')


def test_metric_key_map_is_the_servers_field_table():
    expected = {field: metric for metric, fields in WEARABLE_METRIC_FIELDS.items() for field, _label in fields}
    assert json.loads(PATH.read_text()) == expected
