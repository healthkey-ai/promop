"""Implementation destinations for inventory evidence, without approving concepts.

Source occurrences keep their typed identities and contexts. A catalog route,
frontend consumer or source-method dependency is not a clinical value alias.
"""
from collections import Counter, defaultdict


BC_FIELDS = frozenset('''tumor_stage nodes_stage distant_metastasis_stage
    metastatic_status metastasis_status lymph_node_status bone_only_metastasis_status
    tumor_size staging_modalities histologic_type biopsy_grade biopsy_grade_depr
    menopausal_status ki67_proliferation_index hr_status hrd_status ecog_assessment_date
    test_methodology test_date test_specimen_type oncotype_dx_score androgen_receptor_status
    estrogen_receptor_status progesterone_receptor_status her2_status pd_l1_assay
    pd_l1_tumor_cells pd_l1_combined_positive_score pd_l1_ic_percentage'''.split())
GENETIC_FIELDS = frozenset({'genetic_mutations', 'cytogenetic_markers', 'molecular_markers'})
THERAPY_FIELDS = frozenset({
    'planned_therapies', 'concomitant_medications', 'concomitant_medication_details',
    'concomitant_medication_date', 'supportive_therapies', 'supportive_therapy_date',
    'supportive_therapy_start_date', 'supportive_therapy_end_date', 'supportive_therapy_intent',
    'therapy_overrides', 'therapy_component_ids', 'therapy_type_ids', 'therapy_ids_provenance',
    'therapy_lines_count', 'line_of_therapy',
    *(prefix + suffix for prefix in ('first_line_', 'second_line_', 'later_')
      for suffix in ('therapy', 'therapies', 'therapy_id', 'therapy_ids', 'component_ids',
                     'therapy_type_ids', 'outcome', 'intent', 'discontinuation_reason',
                     'date', 'start_date', 'end_date')),
})


def field_owner(field):
    if field in BC_FIELDS:
        return '#1227'
    if field in GENETIC_FIELDS or field.startswith(('genomics_', 'genetic_mutations.')):
        return '#1229'
    if field in THERAPY_FIELDS:
        return '#1230'
    return '#1228'


def implementation_contracts(rows, crosswalk, bindings, frontend, decisions=(), *, preserve_dispositions_for=()):
    """Attach explicit implementation owners to fresh, unapproved source rows.

    Existing reviewed dispositions and database mapping records are preserved.
    An unresolved context remains unresolved even when its consumers are known.
    """
    field_rows = {r['destination_path']: r for r in rows if r['source'] == 'promop_field'}
    preserved = set(preserve_dispositions_for)
    reviewed_routes = ([] if crosswalk.get('status') == 'source_revision_requires_review'
                       else crosswalk.get('bindings', []))
    routes = {r['option_list']: r for r in reviewed_routes
              if r['status'] != 'unreviewed_binding'}
    methods = defaultdict(set)
    for binding in bindings:
        name = binding['option_list']
        if name not in routes:
            continue
        if binding.get('source_method'):
            methods[binding['source_method']].add(name)
        for dependency in binding.get('static_resolution', {}).get('dependencies', []):
            methods[dependency['method']].add(name)

    constant_files, consumers = defaultdict(set), defaultdict(set)
    for constant in frontend.get('constants', []):
        constant_files[constant.get('root_name', constant['name'])].add(constant['file'])
    for control in frontend.get('controls', []):
        if control.get('constant') and control.get('field'):
            consumers[control['constant']].add(control['field'])

    records = []
    for row in rows:
        field = row['destination_path']
        targets, owners = set(), set()
        route_names = set(row.get('destination_route_keys', [])) & routes.keys()
        if row['source'] == 'cancerbot_source':
            route_names |= methods[row['option_list'].split(':literal:')[0]]
        selected = [routes[name] for name in sorted(route_names)]
        for route in selected:
            targets.update(route['destination_candidates'])
            owners.add(route['owning_issue'])
        representations = {r['representation'] for r in selected}
        disposition = None
        status = 'destination_unresolved'
        reason = 'Destination requires reconciliation; retain source identity and do not project a guessed fact.'
        if selected:
            status = 'source_consumers_recorded'
            reason = 'Use the reviewed source-consumer routes; their contexts and representation conflicts remain distinct.'
            if representations & {'semantic_conflict', 'context_required'}:
                disposition = 'ambiguous'
            elif representations <= {'trial_metadata', 'administrative'}:
                disposition = 'not_applicable'
            elif 'structured' in representations:
                disposition = 'requires_structured_representation'
        elif (row['source'] == 'cancerbot_source' and routes
              and row['option_list'].split(':literal:')[0] in {
                  'trial_types_by_disease_code', 'trial_types_by_purpose_code'}):
            # These reviewed helpers return trial-search filters. Their All
            # sentinels do not assert a patient's treatment or disease state.
            status = 'trial_search_metadata'
            owners.add('#1223')
            disposition = 'not_applicable'
            reason = 'Trial-type filter helper retained as source evidence; no patient clinical answer destination.'
        elif row['source'] == 'promop_catalog':
            targets.add('reference_tables.' + row['option_list'])
            owners.add(row['owning_issue'])
            status = 'existing_reference_catalog'
            reason = 'Curate the existing reference row and its relationships; catalog selection alone does not create a clinical fact.'
        elif row['source'] == 'frontend_constant' and not field:
            file, name = row['option_list'].split(':', 1)
            root_name = name.split('[', 1)[0]
            if constant_files[root_name] == {file}:
                targets.update(consumers[root_name])
            status = 'frontend_consumers_recorded' if targets else 'retained_source_evidence'
            reason = ('The current controls supply consumer fields; descriptor options can override this fallback.'
                      if targets else 'No direct current control consumes this constant in the captured frontend. '
                      'Retain it as legacy/source evidence, not a new runtime choice or proof of retirement.')
            owners.update(field_owner(t) for t in targets)
            if not targets:
                owners.add('#1231')
        if field:
            targets.add(field)
            owners.add(field_owner(field))
            if not selected:
                status = 'field_destination_recorded'
                category = field_rows.get(field, {}).get('descriptor_category')
                reason = ('Use the existing field owner and scoped question/answer recipe; '
                          'a recorded destination does not approve its attached concept candidates.')
                if category in {'profile', 'location', 'internal', 'unit', 'computed', 'alias'}:
                    reason = f'Existing {category} ownership; preserve its resource, event companion or derivation instead of inventing a scalar question.'
                    disposition = 'not_applicable'
                if field in GENETIC_FIELDS or field.startswith(('genomics_', 'genetic_mutations.')):
                    reason = 'Use Genomics findings and linked components; retain marker, gene and variant scope with source-only fallback where required.'
                    disposition = 'requires_structured_representation'
                elif field in THERAPY_FIELDS:
                    reason = 'Use the managed regimen/component/class graph and existing episode writer; preserve line, dates and planned versus administered status.'
                    disposition = 'requires_structured_representation'
        if row['source'] == 'cancerbot_history':
            targets.update(row.get('destination_candidates', []))
            status = 'historical_source_evidence'
            reason = row['reason']
            # A removed source field may have no current destination. Preserve
            # the historical rule instead of making it an active option.
            disposition = None
        matching_decisions = [d for d in decisions if targets.intersection(d['fields'])]
        if matching_decisions and row['source'] != 'cancerbot_history':
            reason += ' ' + ' '.join(d['decision'] for d in matching_decisions)
            owners.update(d['owner'] for d in matching_decisions)
            if not disposition:
                disposition = 'requires_structured_representation'
        missing = sorted(t for t in targets if not t.startswith('reference_tables.')
                         and t not in field_rows and not t.startswith('PersonLanguageSkill.'))
        if missing:
            reason += ' Missing destination fields require the owning implementation; source routing does not create them.'
        if not owners:
            owners.add(row['owning_issue'])
        contract = {'status': status, 'destinations': sorted(targets), 'missing_destinations': missing,
                    'route_keys': sorted(route_names), 'implementation_owners': sorted(owners),
                    'reason': reason, 'semantic_approval': False}
        row['implementation_contract'] = contract
        # Only annotate unresolved inventory states. Never overwrite a reviewed
        # mapping disposition or change the exported database approval records.
        if (row['id'] not in preserved and row.get('reviewer') is None
                and 'source_label_changed' not in row.get('validation_flags', [])
                and row['disposition'] in {'needs_review', 'requires_structured_representation'}):
            if disposition:
                row['disposition'] = disposition
            row['owning_issue'] = next(iter(owners)) if len(owners) == 1 else '#1224'
            row['reason'] = reason
        records.append(contract)
    return {'counts': dict(sorted(Counter(r['status'] for r in records).items())),
            'unresolved_row_ids': [r['id'] for r in rows if r['implementation_contract']['status'] == 'destination_unresolved'],
            'limitation': 'Source/consumer routing assigns implementation work. It does not approve concepts, '
                          'merge aliases, infer retirement or certify runtime field availability.'}
