from omop_core.management.commands.audit_cancerbot_reference import source_reference


def test_source_audit_does_not_execute_cancerbot_and_preserves_typed_options(tmp_path):
    services = tmp_path / 'trials/services'
    services.mkdir(parents=True)
    (services / 'value_options.py').write_text('''
raise RuntimeError("Never execute the reference source")
class ValueOptions:
    def positive_negative(self):
        return {"": "Unknown", True: "Positive", False: "Negative"}
    def get_all_options(self):
        return {"positiveNegative": {"options": self.positive_negative()}}
''')
    result = source_reference(tmp_path)
    options = result['value_options'][0]['literal_values']
    assert options == [{'source_key': '', 'label': 'Unknown', 'disposition': 'needs_review'},
                       {'source_key': True, 'label': 'Positive', 'disposition': 'needs_review'},
                       {'source_key': False, 'label': 'Negative', 'disposition': 'needs_review'}]
    assert result['api_option_bindings'][0]['source_key'] == 'positiveNegative'
    assert result['therapy_crosswalk'] == []
    assert result['limitations']
