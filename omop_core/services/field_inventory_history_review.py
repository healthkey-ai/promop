"""Reviewed scopes of CancerBot migration definitions.

These classifications account for source history. They never certify execution,
clinical equivalence or approval to replay a source repair in PRomop.
"""

HISTORY_SCOPES = {'0020_alter_studyinfo_intervention_treatment.py': {'status': 'trial_search_metadata',
                                                    'reason': 'Trial search, extraction prompts or '
                                                              'eligibility metadata. Preserve this scope; '
                                                              'these changes do not define patient answer '
                                                              'aliases or retire therapy catalogs.',
                                                    'owning_issue': '#1223',
                                                    'sha256': '5bd8f60bc3248b814eecd04d28a88da314793ba3a6dbdc02ce196bc043fd63e3'},
 '0021_auto_20241101_2053.py': {'status': 'trial_search_metadata',
                                'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                          'Preserve this scope; these changes do not define patient answer '
                                          'aliases or retire therapy catalogs.',
                                'owning_issue': '#1223',
                                'sha256': '0e0d7e9b174b9a81d5d9d7c483e58a07981a1f5b67ac67b66ef46d7c2d3627ed'},
 '0023_alter_patientinfo_substance_use_status_and_more.py': {'status': 'historical_lossy_conversion',
                                                             'reason': 'Converts historical substance-use '
                                                                       'text with Python bool into a '
                                                                       'temporary boolean. Any nonempty text '
                                                                       'becomes true; the lost distinctions '
                                                                       'cannot be reconstructed or approved '
                                                                       'as clinical aliases.',
                                                             'owning_issue': '#1231',
                                                             'sha256': '6ac312681ed5cc1e036aa35dc5b255919b0ec4b680900158e3d5f453094f428b'},
 '0075_populate_fieldparticipationrequirement_key.py': {'status': 'trial_search_metadata',
                                                        'reason': 'Trial search, extraction prompts or '
                                                                  'eligibility metadata. Preserve this '
                                                                  'scope; these changes do not define '
                                                                  'patient answer aliases or retire therapy '
                                                                  'catalogs.',
                                                        'owning_issue': '#1223',
                                                        'sha256': '84e414cbf5f82b8252b6a2a57d14c844f96bc4a4ca73997e7b821ad95a63132b'},
 '0108_trial_plasma_cell_leukemia_required.py': {'status': 'trial_search_metadata',
                                                 'reason': 'Trial search, extraction prompts or eligibility '
                                                           'metadata. Preserve this scope; these changes do '
                                                           'not define patient answer aliases or retire '
                                                           'therapy catalogs.',
                                                 'owning_issue': '#1223',
                                                 'sha256': '6ec2a181471597b4700a67c7db1c1c88a194e42dc1a2f4347bd8aab8ec019cba'},
 '0110_trial_prior_therapy_lines.py': {'status': 'trial_search_metadata',
                                       'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                                 'Preserve this scope; these changes do not define patient '
                                                 'answer aliases or retire therapy catalogs.',
                                       'owning_issue': '#1223',
                                       'sha256': '60cfc3f788fd5aa30d4560be1d80dc5d69aa8920c069ba10667c64cfd237b1e5'},
 '0120_add_therapy_prompts.py': {'status': 'trial_search_metadata',
                                 'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                           'Preserve this scope; these changes do not define patient answer '
                                           'aliases or retire therapy catalogs.',
                                 'owning_issue': '#1223',
                                 'sha256': 'a28e9dd55d808216406124553a73d00f5bae25d22963a5b426ce0d52540b320f'},
 '0170_alter_trial_code.py': {'status': 'trial_search_metadata',
                              'reason': 'Trial search, extraction prompts or eligibility metadata. Preserve '
                                        'this scope; these changes do not define patient answer aliases or '
                                        'retire therapy catalogs.',
                              'owning_issue': '#1223',
                              'sha256': '2c8db7d2fc9e4e45d5594c774ba2af7008f08d96b61a03dc6e34565478cba39d'},
 '0174_patch_prompts_and_labeled_values.py': {'status': 'trial_search_metadata',
                                              'reason': 'Trial search, extraction prompts or eligibility '
                                                        'metadata. Preserve this scope; these changes do not '
                                                        'define patient answer aliases or retire therapy '
                                                        'catalogs.',
                                              'owning_issue': '#1223',
                                              'sha256': 'bfacb5760bb325a7d93be33d212e6352211fcf07089c820878af1f67f6f209d7'},
 '0240_epicendpoint_alter_epicorganization_endpoint_and_more.py': {'status': 'platform_metadata',
                                                                   'reason': 'Endpoint, help-text, '
                                                                             'scheduler, permission-label or '
                                                                             'projection infrastructure '
                                                                             'metadata; no patient '
                                                                             'controlled-value equivalence '
                                                                             'is established.',
                                                                   'owning_issue': '#1223',
                                                                   'sha256': 'cfadb4724a95e07db9ad85ac3fc4c14ce00ca71ab8324f1bd512084234e90578'},
 '0266_re_seed_planned_therapies.py': {'status': 'delegated_reference_seed',
                                       'reason': 'Seeds the separate PlannedTherapy catalog and disease '
                                                 'links from mutable loader methods. Lowercase disease '
                                                 'deletion and code normalization are scoped source rules, '
                                                 'not global aliases. Current loader hashes and live '
                                                 'eligibility are recorded; historical execution inputs are '
                                                 'unavailable.',
                                       'owning_issue': '#1230',
                                       'sha256': '89f5688c43228c07b50d4981015b30f21da81bbfcbabb46bcfacaff446002a0a'},
 '0270_re_seed_tnm_options.py': {'status': 'delegated_reference_seed',
                                 'reason': 'Runs LoadTnmOptions.load_all. Current loader dependencies are '
                                           'fingerprinted and current reference options are exported; '
                                           'historical execution inputs and deployments are not certified. '
                                           'Preserve staging axis, system and basis.',
                                 'owning_issue': '#1227',
                                 'sha256': 'ead5a0feff3bd129f18749baaf6faeaf77cbc9b88fb808a7bd0d2e1fdbaf7bc0'},
 '0286_trial_lay_summary.py': {'status': 'trial_search_metadata',
                               'reason': 'Trial search, extraction prompts or eligibility metadata. Preserve '
                                         'this scope; these changes do not define patient answer aliases or '
                                         'retire therapy catalogs.',
                               'owning_issue': '#1223',
                               'sha256': 'd2101b6c359f173d17769e9be7f92b3f430f0b8e350e920d51eba5207b31f2f1'},
 '0304_seed_trial_type_diseases.py': {'status': 'trial_search_metadata',
                                      'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                                'Preserve this scope; these changes do not define patient '
                                                'answer aliases or retire therapy catalogs.',
                                      'owning_issue': '#1223',
                                      'sha256': '64e24ac2e410c1796fe15adb89a775febd38b71d49845e4666b1aeea77ed29d2'},
 '0306_add_cll_disease.py': {'status': 'catalog_addition',
                             'reason': 'Adds the CLL Disease code/title when absent. Reverse removal is a '
                                       'rollback definition, not evidence of current retirement.',
                             'owning_issue': '#1223',
                             'sha256': '96002f8d6b028803f8369b2814069d3a81eac276263271a7087e076b3f12d029'},
 '0314_remove_patienttrialsoccomparison_soc_benefit_score_and_more.py': {'status': 'derived_comparison_cache',
                                                                         'reason': 'Standard-of-care '
                                                                                   'comparison caches are '
                                                                                   'cleared. These are not '
                                                                                   'patient findings or '
                                                                                   'managed '
                                                                                   'regimen/component/class '
                                                                                   'catalog removals.',
                                                                         'owning_issue': '#1223',
                                                                         'sha256': 'd001b7bd102c559770fc4d51c6a9f1848a8b94bc6808562e1acf87ca3c32c20c'},
 '0331_seed_cll_trial_types.py': {'status': 'trial_search_metadata',
                                  'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                            'Preserve this scope; these changes do not define patient answer '
                                            'aliases or retire therapy catalogs.',
                                  'owning_issue': '#1223',
                                  'sha256': '333d4873e482380412987209c7da9e2f62cc95c3d2f9ef881d307d7cc8e9fb1c'},
 '0333_seed_eu_preferred_countries.py': {'status': 'administrative_reference_seed',
                                         'reason': 'PreferredCountry code/title/sort-key seed. Current '
                                                   'country membership remains the live reference snapshot; '
                                                   'no clinical concept is created.',
                                         'owning_issue': '#1223',
                                         'sha256': '5b3e6d0981aa8eaf739459b27257cec8d100fd104a270d945a0f9e0a6d129d37'},
 '0336_seed_au_nz_preferred_countries.py': {'status': 'administrative_reference_seed',
                                            'reason': 'PreferredCountry code/title/sort-key seed. Current '
                                                      'country membership remains the live reference '
                                                      'snapshot; no clinical concept is created.',
                                            'owning_issue': '#1223',
                                            'sha256': 'ea7d3368e424ceb4ed35dc466a1c9ebf79a54056e9a11cf44f4c36cd52824eb8'},
 '0339_wipe_soc_data.py': {'status': 'derived_comparison_cache',
                           'reason': 'Standard-of-care comparison caches are cleared. These are not patient '
                                     'findings or managed regimen/component/class catalog removals.',
                           'owning_issue': '#1223',
                           'sha256': '4c21a8349a3cb949b395dba88f80f1b26c408f01e868d33e8f518e630f190d11'},
 '0342_wipe_therapy_soc_data.py': {'status': 'derived_comparison_cache',
                                   'reason': 'Standard-of-care comparison caches are cleared. These are not '
                                             'patient findings or managed regimen/component/class catalog '
                                             'removals.',
                                   'owning_issue': '#1223',
                                   'sha256': '374575d935083406ed31587da6daca195b97470b04f80cda824a607b042549f3'},
 '0344_seed_trial_purposes.py': {'status': 'trial_search_metadata',
                                 'reason': 'Trial search, extraction prompts or eligibility metadata. '
                                           'Preserve this scope; these changes do not define patient answer '
                                           'aliases or retire therapy catalogs.',
                                 'owning_issue': '#1223',
                                 'sha256': '38ef08d76a952cabffc0bc7217ef3708cce1a65465d1be1e8af270097d7f5e30'},
 '0344_seed_trialpurpose.py': {'status': 'trial_search_metadata',
                               'reason': 'Trial search, extraction prompts or eligibility metadata. Preserve '
                                         'this scope; these changes do not define patient answer aliases or '
                                         'retire therapy catalogs.',
                               'owning_issue': '#1223',
                               'sha256': '8fd984077d40e1490ee44c0f3329953fe3680b601baec37298697d6717f97810'},
 '0345_add_trial_purpose_fk_and_requirement.py': {'status': 'trial_search_metadata',
                                                  'reason': 'Trial search, extraction prompts or eligibility '
                                                            'metadata. Preserve this scope; these changes do '
                                                            'not define patient answer aliases or retire '
                                                            'therapy catalogs.',
                                                  'owning_issue': '#1223',
                                                  'sha256': 'df05a78e105773e7d0906cd2f319f7092ee69cdd48fa244d519c84209ac5f7af'},
 '0349_seed_trial_types_from_taxonomy.py': {'status': 'trial_search_metadata',
                                            'reason': 'Trial search, extraction prompts or eligibility '
                                                      'metadata. Preserve this scope; these changes do not '
                                                      'define patient answer aliases or retire therapy '
                                                      'catalogs.',
                                            'owning_issue': '#1223',
                                            'sha256': '0cf7867ab5a3f0523605a8bdcfb841cc9fc713762c1ccced1658de7dfda351f3'},
 '0354_seed_help_text.py': {'status': 'platform_metadata',
                            'reason': 'Endpoint, help-text, scheduler, permission-label or projection '
                                      'infrastructure metadata; no patient controlled-value equivalence is '
                                      'established.',
                            'owning_issue': '#1223',
                            'sha256': '5b2c49e0292398672d23c156cc92ddd200e88e34e60cd12cad3a9491cb3a1f6b'},
 '0374_therapy_required_array_constraint.py': {'status': 'trial_search_metadata',
                                               'reason': 'Trial search, extraction prompts or eligibility '
                                                         'metadata. Preserve this scope; these changes do '
                                                         'not define patient answer aliases or retire '
                                                         'therapy catalogs.',
                                               'owning_issue': '#1223',
                                               'sha256': '18240b32505cd0c4f1e49054df5ed7214ae264fcf2129d23bea96dc4cb1f1aee'},
 '0382_cytogenicmarker_molecularmarker_and_more.py': {'status': 'delegated_reference_seed',
                                                      'reason': 'Replaces the old Marker/category schema '
                                                                'with CytogenicMarker and MolecularMarker '
                                                                'using a mutable loader. Current dependency '
                                                                'hashes and reference options are available; '
                                                                'no equivalence between arbitrary old and '
                                                                'new marker codes is inferred.',
                                                      'owning_issue': '#1229',
                                                      'sha256': '273681abd1fbb4a12302bb07b1d19fc9585146a6bb739cca7e58352fad3ac068'},
 '0383_high_risk_mcl_criteria_extraction_prompts.py': {'status': 'trial_search_metadata',
                                                       'reason': 'Trial search, extraction prompts or '
                                                                 'eligibility metadata. Preserve this scope; '
                                                                 'these changes do not define patient answer '
                                                                 'aliases or retire therapy catalogs.',
                                                       'owning_issue': '#1223',
                                                       'sha256': '7cb60e4056ef9363a8d3b17e72bf4b30975972783373bc790abc533e631498fc'},
 '0384_fix_miscased_extractor_prompt_keys.py': {'status': 'trial_search_metadata',
                                                'reason': 'Trial search, extraction prompts or eligibility '
                                                          'metadata. Preserve this scope; these changes do '
                                                          'not define patient answer aliases or retire '
                                                          'therapy catalogs.',
                                                'owning_issue': '#1223',
                                                'sha256': 'd1f57351a7bd9b40b77916441243268f02890386841736fdd83aae8e16b4159e'},
 '0394_nct06558604_high_mipi_simplified.py': {'status': 'trial_search_metadata',
                                              'reason': 'Trial search, extraction prompts or eligibility '
                                                        'metadata. Preserve this scope; these changes do not '
                                                        'define patient answer aliases or retire therapy '
                                                        'catalogs.',
                                              'owning_issue': '#1223',
                                              'sha256': '5e31f828577956409754efa00c0aa9de88b078b6ab83ef239d7a7770e7c69367'},
 '0398_remove_trial_idx_omop_ther_types_pair_gin_and_more.py': {'status': 'superseded_class_mapping_clear',
                                                                'reason': 'Historical CancerBot '
                                                                          'class-concept clearing used a '
                                                                          'premise explicitly superseded by '
                                                                          'PRomop ADR 0002 and CancerBot '
                                                                          '0409/0410. Preserve current class '
                                                                          'mapping capabilities; do not '
                                                                          'replay this clearing into PRomop.',
                                                                'owning_issue': '#1230',
                                                                'sha256': '202fe26b47fe647a375aa325f0e0b8719b2928ffc6a589b02b30d9b18dcf02b4'},
 '0399_componentcategoryomoplookup.py': {'status': 'derived_reference_lookup',
                                         'reason': 'Builds component-concept to category-code lookup from '
                                                   'existing non-null component/category links. This does '
                                                   'not approve class concepts or establish a new '
                                                   'authoritative therapy catalog.',
                                         'owning_issue': '#1230',
                                         'sha256': '0bd0ddd3c2e7daedf3fd4c6bb621f9f3f160b143c2de2a081742a5daa18d3594'},
 '0401_alter_patientinfo_white_blood_cell_count_units.py': {'status': 'historical_unit_repair',
                                                            'reason': 'X10E9/L counts are multiplied by '
                                                                      '1000; other nonblank noncanonical '
                                                                      'labels are relabeled CELLS/UL, and '
                                                                      'MIPI is recomputed using the mutable '
                                                                      'live attributes service. Record as '
                                                                      'source repair history, never a '
                                                                      'universal unit alias or authority to '
                                                                      'rescale current PRomop values.',
                                                            'owning_issue': '#1231',
                                                            'sha256': '9e143c3287d2c6725a38d5a7aa60c9956d7a8d1c648106bb7629fae99ecf91e3'},
 '0402_delete_orphaned_refresh_trial_statuses_beat_task.py': {'status': 'platform_metadata',
                                                              'reason': 'Endpoint, help-text, scheduler, '
                                                                        'permission-label or projection '
                                                                        'infrastructure metadata; no patient '
                                                                        'controlled-value equivalence is '
                                                                        'established.',
                                                              'owning_issue': '#1223',
                                                              'sha256': 'a2d8834d943bcb86a3bb23289101a75ae468312a8c2816d28e9673c5aa20475c'},
 '0409_studyinfo_backfill_trial_purpose.py': {'status': 'trial_search_metadata',
                                              'reason': 'Trial search, extraction prompts or eligibility '
                                                        'metadata. Preserve this scope; these changes do not '
                                                        'define patient answer aliases or retire therapy '
                                                        'catalogs.',
                                              'owning_issue': '#1223',
                                              'sha256': 'eaab20d504867a911374463eb0c446e049be0fc37074914e33d656d32d7ad30c'},
 '0410_help_text_additional_treatment.py': {'status': 'platform_metadata',
                                            'reason': 'Endpoint, help-text, scheduler, permission-label or '
                                                      'projection infrastructure metadata; no patient '
                                                      'controlled-value equivalence is established.',
                                            'owning_issue': '#1223',
                                            'sha256': '0d228dc24d7b6998fa0cc1746c0728a80cdecf3222f80d6bf8f19fe14fd6a785'},
 '0411_help_text_soc_link.py': {'status': 'platform_metadata',
                                'reason': 'Endpoint, help-text, scheduler, permission-label or projection '
                                          'infrastructure metadata; no patient controlled-value equivalence '
                                          'is established.',
                                'owning_issue': '#1223',
                                'sha256': '51fdfe8de9c6475b6f9648226b3c24fac33acbb94bc8866769f760ecaa3625a6'},
 '0412_help_text_soc_score_breakdown.py': {'status': 'platform_metadata',
                                           'reason': 'Endpoint, help-text, scheduler, permission-label or '
                                                     'projection infrastructure metadata; no patient '
                                                     'controlled-value equivalence is established.',
                                           'owning_issue': '#1223',
                                           'sha256': '3061ae7382f16d5c4fa6be7bd58c941580e3c1b900a66b46c7724d63a9a48482'},
 '0413_studyinfo_trial_purpose_multi.py': {'status': 'trial_search_metadata',
                                           'reason': 'Trial search, extraction prompts or eligibility '
                                                     'metadata. Preserve this scope; these changes do not '
                                                     'define patient answer aliases or retire therapy '
                                                     'catalogs.',
                                           'owning_issue': '#1223',
                                           'sha256': '8f55b162bcfcf91a96c98b9e627c623a370c555dd1b48e4bcd70330e4633f655'},
 '0414_cytogenetic_marker_verbose_name.py': {'status': 'platform_metadata',
                                             'reason': 'Endpoint, help-text, scheduler, permission-label or '
                                                       'projection infrastructure metadata; no patient '
                                                       'controlled-value equivalence is established.',
                                             'owning_issue': '#1223',
                                             'sha256': '85bae3acdcd4d8e3397d7290457ad6573541d79d0a6c70c9acbae76732b89aeb'},
 '0415_projectionrelease_projectionsnapshot.py': {'status': 'projection_snapshot_integrity',
                                                  'reason': 'RunSQL installs projection snapshot '
                                                            'immutability and release-transition '
                                                            'enforcement; no clinical option alias is '
                                                            'established.',
                                                  'owning_issue': '#1223',
                                                  'sha256': '68bee17d81aabae45da2f809ebae2058e186b1e6ccdb04ee834f8db515704565'},
 '0416_anc_legacy_units_4764.py': {'status': 'historical_unit_repair',
                                   'reason': 'Only distinguishable x10^9/L aliases with a count are rescaled '
                                             'by 1000. Other legacy labels are relabeled CELLS/UL; '
                                             'already-canonical low values are untouched. This lossy repair '
                                             'is not evidence of individual patient units or a reversible '
                                             'clinical conversion.',
                                   'owning_issue': '#1231',
                                   'sha256': '92e69c083f1c00373bb6cff586cebae52a8cf4e290d3942e99698cd5bcf8dffb'},
 '0418_calcium_units_mmol_l.py': {'status': 'historical_unit_label_repair',
                                  'reason': 'CancerBot renamed MICROMOLES/L to MMOL/L without rescaling. '
                                            'Preserve the source-specific correction history; do not apply '
                                            'it as a universal conversion from micromoles to millimoles.',
                                  'owning_issue': '#1231',
                                  'sha256': '94cba11d702dad652f897f587704f3c7838b7577ef545e0a3b64d6fe195ebd6c'},
 '0418_help_text_not_evaluated_4850.py': {'status': 'trial_search_metadata',
                                          'reason': 'Updates trial eligibility help text to distinguish '
                                                    'not-evaluated criteria. This is not a new patient '
                                                    'result value or a negative clinical finding.',
                                          'owning_issue': '#1223',
                                          'sha256': 'b298bf64f553c0de6490afb43e3dc44f69cfe26eb4aa0a90bb290fb5ec8d247e'}}
