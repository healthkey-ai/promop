import { useVocabulary } from '@/hooks/useVocabulary';
import { useWritableFields } from '@/hooks/useWritableFields';
import ClinicalField from '../ClinicalField';
import Section from '../Section';
import FlipiAssessment from '../FlipiAssessment';
import GelfAssessment from '../GelfAssessment';
import {
  STAGE_OPTIONS, HISTOLOGIC_TYPE_OPTIONS,
  MENOPAUSAL_OPTIONS, TUMOR_STAGE_OPTIONS, NODES_STAGE_OPTIONS,
  STAGING_MODALITIES_OPTIONS, DISTANT_METASTASIS_STAGE_OPTIONS,
  YES_NO_OPTIONS, ER_OPTIONS, PR_OPTIONS, HER2_OPTIONS, HR_OPTIONS, HRD_OPTIONS,
  DISEASE_OPTIONS,
  FL_TUMOR_GRADE_OPTIONS,
  ISS_STAGE_OPTIONS, MM_PROGRESSION_OPTIONS, STEM_CELL_TRANSPLANT_OPTIONS, SCT_ELIGIBILITY_OPTIONS, MYELOMA_TYPE_OPTIONS,
  MRD_STATUS_OPTIONS,
  BINET_STAGE_OPTIONS, TUMOR_BURDEN_OPTIONS, DISEASE_ACTIVITY_OPTIONS,
  RICHTER_TRANSFORMATION_OPTIONS, PROTEIN_EXPRESSION_OPTIONS,
} from '../patientConstants';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'mcl' | 'other';
}

function BreastCancerSection({ formData, onChange }: Omit<Props, 'diseaseType'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const { source: erSource }            = useVocabulary('estrogen-receptor-status', 'title');
  const { source: prSource }            = useVocabulary('progesterone-receptor-status', 'title');
  const { source: her2Source }          = useVocabulary('her2-status', 'title');
  const { source: hrSource }            = useVocabulary('hr-status', 'title');
  const { source: hrdSource }           = useVocabulary('hrd-status', 'title');
  const { source: tumorStageSource }    = useVocabulary('tumor-stage', 'title');
  const { source: nodesStageSource }    = useVocabulary('nodes-stage', 'title');
  const { source: distantMetSource }    = useVocabulary('distant-metastasis-stage', 'title');
  const { source: stagingModalitySource } = useVocabulary('staging-modality', 'title');
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');

  const histOptions = histologicOptions.length ? histologicOptions.map((o: { value: string }) => o.value) : HISTOLOGIC_TYPE_OPTIONS;

  return (
    <>
      <Section title="Tumor Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <ClinicalField label="Histologic Type" name="histologic_type" descriptor={descriptors.histologic_type} type="select" value={formData?.histologic_type} options={histOptions} onChange={onChange} vocabSource={histologicSource} />
          </div>
          <ClinicalField label="Histologic Grade" name="tumor_grade" descriptor={descriptors.tumor_grade} type="select" value={formData?.tumor_grade} options={['1', '2', '3']} onChange={onChange} />
          <ClinicalField label="Menopausal Status" name="menopausal_status" descriptor={descriptors.menopausal_status} type="select" value={formData?.menopausal_status} options={MENOPAUSAL_OPTIONS} onChange={onChange} />
          <ClinicalField label="Tumor Stage" name="tumor_stage" descriptor={descriptors.tumor_stage} type="select" value={formData?.tumor_stage} options={TUMOR_STAGE_OPTIONS} onChange={onChange} vocabSource={tumorStageSource} />
          <ClinicalField label="Nodes Stage" name="nodes_stage" descriptor={descriptors.nodes_stage} type="select" value={formData?.nodes_stage} options={NODES_STAGE_OPTIONS} onChange={onChange} vocabSource={nodesStageSource} />
          <ClinicalField label="Staging Modalities" name="staging_modalities" descriptor={descriptors.staging_modalities} type="select" value={formData?.staging_modalities} options={STAGING_MODALITIES_OPTIONS} onChange={onChange} vocabSource={stagingModalitySource} />
          <ClinicalField label="Distant Metastasis Stage" name="distant_metastasis_stage" descriptor={descriptors.distant_metastasis_stage} type="select" value={formData?.distant_metastasis_stage} options={DISTANT_METASTASIS_STAGE_OPTIONS} onChange={onChange} vocabSource={distantMetSource} />
          <ClinicalField label="Bone-Only Metastasis" name="bone_only_metastasis_status" descriptor={descriptors.bone_only_metastasis_status} type="boolean" value={formData?.bone_only_metastasis_status} onChange={onChange} />
          <ClinicalField label="Measurable Disease by RECIST" name="measurable_disease_by_recist_status" descriptor={descriptors.measurable_disease_by_recist_status} type="boolean" value={formData?.measurable_disease_by_recist_status} onChange={onChange} />
        </div>
      </Section>

      <Section title="Receptor Status">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Estrogen Receptor (ER) Status" name="estrogen_receptor_status" descriptor={descriptors.estrogen_receptor_status} type="select" value={formData?.estrogen_receptor_status} options={ER_OPTIONS} onChange={onChange} vocabSource={erSource} />
          <ClinicalField label="Progesterone Receptor (PR) Status" name="progesterone_receptor_status" descriptor={descriptors.progesterone_receptor_status} type="select" value={formData?.progesterone_receptor_status} options={PR_OPTIONS} onChange={onChange} vocabSource={prSource} />
          <ClinicalField label="HER2 Status" name="her2_status" descriptor={descriptors.her2_status} type="select" value={formData?.her2_status} options={HER2_OPTIONS} onChange={onChange} vocabSource={her2Source} />
          <ClinicalField label="HR Status" name="hr_status" descriptor={descriptors.hr_status} type="select" value={formData?.hr_status} options={HR_OPTIONS} onChange={onChange} vocabSource={hrSource} />
          <ClinicalField label="HRD Status" name="hrd_status" descriptor={descriptors.hrd_status} type="select" value={formData?.hrd_status} options={HRD_OPTIONS} onChange={onChange} vocabSource={hrdSource} />
          <ClinicalField label="Androgen Receptor Status" name="androgen_receptor_status" descriptor={descriptors.androgen_receptor_status} type="select" value={formData?.androgen_receptor_status} options={ER_OPTIONS} onChange={onChange} />
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Triple Negative Status (Computed)</label>
            <div className="flex h-9 w-full items-center rounded-md border border-input bg-portal-bg-secondary px-3 py-2 text-sm text-portal-text-tertiary">
              {formData?.tnbc_status ? 'Yes' : 'No'}
            </div>
            <p className="text-xs text-portal-text-tertiary">Automatically computed from ER, PR, and HER2 status</p>
          </div>
        </div>
      </Section>

      <Section title="Additional Biomarkers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Ki-67 Proliferation Index (%)" name="ki67_proliferation_index" descriptor={descriptors.ki67_proliferation_index} type="number" value={formData?.ki67_proliferation_index} onChange={onChange} />
          <ClinicalField label="PD-L1 Status (%)" name="pd_l1_tumor_cells" descriptor={descriptors.pd_l1_tumor_cells} type="number" value={formData?.pd_l1_tumor_cells} onChange={onChange} />
          <ClinicalField label="Oncotype DX Score" name="oncotype_dx_score" descriptor={descriptors.oncotype_dx_score} type="number" value={formData?.oncotype_dx_score} onChange={onChange} />
        </div>
      </Section>

      <Section title="Test Information">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <ClinicalField label="Test Methodology" name="test_methodology" descriptor={descriptors.test_methodology} type="text" value={formData?.test_methodology} onChange={onChange} />
          </div>
          <ClinicalField label="Test Date" name="test_date" descriptor={descriptors.test_date} type="date" value={formData?.test_date} onChange={onChange} />
          <div className="sm:col-span-2">
            <ClinicalField label="Test Specimen Type" name="test_specimen_type" descriptor={descriptors.test_specimen_type} type="text" value={formData?.test_specimen_type} onChange={onChange} />
          </div>
          <div className="sm:col-span-2">
            <ClinicalField label="Report Interpretation" name="report_interpretation" descriptor={descriptors.report_interpretation} type="text" value={formData?.report_interpretation} onChange={onChange} />
          </div>
        </div>
      </Section>


    </>
  );
}

function LymphomaSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const { source: flGradeSource } = useVocabulary('follicular-lymphoma-grade', 'title');
  const { options: txOutcomeOptions, source: txOutcomeSource } = useVocabulary('post-transformation-outcome', 'title');
  return (
    <>
      <Section title="Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Ann Arbor Stage" name="stage" descriptor={descriptors.stage} type="select" value={formData?.stage} options={STAGE_OPTIONS} onChange={onChange} />
          <ClinicalField label="Tumor Grade" name="tumor_grade" descriptor={descriptors.tumor_grade} type="select" value={formData?.tumor_grade} options={FL_TUMOR_GRADE_OPTIONS} onChange={onChange} vocabSource={flGradeSource} />
          <div className="sm:col-span-2"><GelfAssessment value={formData?.gelf_criteria_options} descriptor={descriptors.gelf_criteria_options} onChange={onChange} /></div>
          <div className="sm:col-span-2">
            <FlipiAssessment value={formData?.flipi_score_options} descriptor={descriptors.flipi_score_options} onChange={onChange} />
          </div>
          <ClinicalField label="Bulky Disease" name="bulky_disease" descriptor={descriptors.bulky_disease} type="select" value={formData?.bulky_disease} options={YES_NO_OPTIONS} onChange={onChange} />
          <ClinicalField label="B Symptoms" name="b_symptoms" descriptor={descriptors.b_symptoms} type="select" value={formData?.b_symptoms} options={YES_NO_OPTIONS} onChange={onChange} />
        </div>
      </Section>

      <Section title="Transformation to DLBCL">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Transformed to DLBCL" name="transformed_to_dlbcl" descriptor={descriptors.transformed_to_dlbcl} type="boolean" value={formData?.transformed_to_dlbcl} onChange={onChange} />
          <ClinicalField label="Transformation Date" name="dlbcl_transformation_date" descriptor={descriptors.dlbcl_transformation_date} type="date" value={formData?.dlbcl_transformation_date} onChange={onChange} />
          <ClinicalField label="Post-Transformation Outcome" name="post_transformation_outcome" descriptor={descriptors.post_transformation_outcome} type="select" value={formData?.post_transformation_outcome} options={txOutcomeOptions.length ? txOutcomeOptions.map((o: { value: string }) => o.value) : []} onChange={onChange} vocabSource={txOutcomeSource} />
        </div>
      </Section>

      <Section title="Laboratory Markers">
        <p className="mb-4 text-sm text-slate-500">Record hemoglobin in Blood and LDH in Labs.</p>
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Bone Marrow Involvement" name="bone_marrow_involvement" descriptor={descriptors.bone_marrow_involvement} type="boolean" value={formData?.bone_marrow_involvement} onChange={onChange} />
          <ClinicalField label="Clonal Bone Marrow B Lymphocytes (%)" name="clonal_bone_marrow_b_lymphocytes" descriptor={descriptors.clonal_bone_marrow_b_lymphocytes} type="number" value={formData?.clonal_bone_marrow_b_lymphocytes} onChange={onChange} />
          <ClinicalField label="LDH Upper Limit of Normal (U/L)" name="ldh_upper_limit_normal" descriptor={descriptors.ldh_upper_limit_normal} type="number" value={formData?.ldh_upper_limit_normal} onChange={onChange} />
          <ClinicalField label="Number of Nodal Sites" name="number_of_nodal_sites" descriptor={descriptors.number_of_nodal_sites} type="number" value={formData?.number_of_nodal_sites} onChange={onChange} />
        </div>
      </Section>
    </>
  );
}

function MyelomaSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const { source: progressionSource } = useVocabulary('disease-progression', 'title');
  const { options: sctTypeOptions, source: sctTypeSource } = useVocabulary('stem-cell-transplant', 'title');
  const { options: sctEligibilityOptions, source: sctEligibilitySource } = useVocabulary('sct-eligibility', 'title');
  const { options: myelomaTypeOptions, source: myelomaTypeSource } = useVocabulary('myeloma-type', 'title');

  return (
    <>
      <Section title="Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="M-Protein Type" name="myeloma_type" descriptor={descriptors.myeloma_type} type="select" value={formData?.myeloma_type} options={myelomaTypeOptions.length ? myelomaTypeOptions.map((o: { value: string }) => o.value) : MYELOMA_TYPE_OPTIONS} onChange={onChange} vocabSource={myelomaTypeSource} />
          <ClinicalField label="Recorded ISS / R-ISS Stage" name="stage" descriptor={descriptors.stage} type="select" value={formData?.stage} options={ISS_STAGE_OPTIONS} onChange={onChange} />
          <ClinicalField label="Progression Status" name="progression" descriptor={descriptors.progression} type="select" value={formData?.progression} options={MM_PROGRESSION_OPTIONS} onChange={onChange} vocabSource={progressionSource} />
          <ClinicalField label="Measurable Disease (IMWG)" name="measurable_disease_imwg" descriptor={descriptors.measurable_disease_imwg} type="boolean" value={formData?.measurable_disease_imwg} onChange={onChange} />
          <ClinicalField label="MRD Status" name="mrd_status" descriptor={descriptors.mrd_status} type="select" value={formData?.mrd_status} options={MRD_STATUS_OPTIONS} onChange={onChange} />
          <ClinicalField label="Meets CRAB Criteria" name="meets_crab" descriptor={descriptors.meets_crab} type="boolean" value={formData?.meets_crab} onChange={onChange} />
          <ClinicalField label="Meets SLiM Criteria" name="meets_slim" descriptor={descriptors.meets_slim} type="boolean" value={formData?.meets_slim} onChange={onChange} />
          <div className="sm:col-span-2">
            <ClinicalField label="Prior SCT Type" name="stem_cell_transplant_history" descriptor={descriptors.stem_cell_transplant_history} type="multiselect" value={formData?.stem_cell_transplant_history} options={sctTypeOptions.length ? sctTypeOptions.map((o: { value: string }) => o.value) : STEM_CELL_TRANSPLANT_OPTIONS} onChange={onChange} vocabSource={sctTypeSource} />
          </div>
          <ClinicalField label="SCT Date" name="sct_date" descriptor={descriptors.sct_date} type="date" value={formData?.sct_date} onChange={onChange} />
          <div className="sm:col-span-2">
            <ClinicalField label="SCT Eligibility" name="sct_eligibility" descriptor={descriptors.sct_eligibility} type="multiselect" value={formData?.sct_eligibility} options={sctEligibilityOptions.length ? sctEligibilityOptions.map((o: { value: string }) => o.value) : SCT_ELIGIBILITY_OPTIONS} onChange={onChange} vocabSource={sctEligibilitySource} />
          </div>
        </div>
      </Section>

      <Section title="Myeloma Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Serum M-Protein (g/dL)" name="monoclonal_protein_serum" descriptor={descriptors.monoclonal_protein_serum} type="number" value={formData?.monoclonal_protein_serum} onChange={onChange} />
          <ClinicalField label="Urine M-Protein (mg/24h)" name="monoclonal_protein_urine" descriptor={descriptors.monoclonal_protein_urine} type="number" value={formData?.monoclonal_protein_urine} onChange={onChange} />
          <ClinicalField label="Kappa Free Light Chains" name="kappa_flc" descriptor={descriptors.kappa_flc} type="number" value={formData?.kappa_flc} onChange={onChange} />
          <ClinicalField label="Lambda Free Light Chains" name="lambda_flc" descriptor={descriptors.lambda_flc} type="number" value={formData?.lambda_flc} onChange={onChange} />
          <ClinicalField label="Kappa/Lambda Ratio" name="kappa_lambda_ratio" descriptor={descriptors.kappa_lambda_ratio} type="number" value={formData?.kappa_lambda_ratio} onChange={onChange} />
          <ClinicalField label="Involved/Uninvolved Ratio" name="involved_uninvolved_ratio" descriptor={descriptors.involved_uninvolved_ratio} type="number" value={formData?.involved_uninvolved_ratio} onChange={onChange} />
        </div>
      </Section>

      <Section title="Complications">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Bone Lesions" name="bone_lesions" descriptor={descriptors.bone_lesions} type="select" value={formData?.bone_lesions} options={YES_NO_OPTIONS} onChange={onChange} />
          <ClinicalField label="Bone Marrow Plasma Cells (%)" name="clonal_plasma_cells" descriptor={descriptors.clonal_plasma_cells} type="number" value={formData?.clonal_plasma_cells} onChange={onChange} />
        </div>
      </Section>

      <Section title="Cytogenetics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <div data-patient-field="cytogenetic_markers">
              <p className="text-sm font-medium">Legacy cytogenetic summary</p>
              <p className="text-sm whitespace-pre-wrap">{String(formData?.cytogenetic_markers || 'No legacy summary recorded.')}</p>
              <p className="text-sm text-muted-foreground">Record individual findings in Genomics. Original legacy results are available there for review.</p>
            </div>
          </div>
        </div>
      </Section>
    </>
  );
}

function CLLSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  return (
    <>
      <Section title="CLL Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Binet Stage" name="binet_stage" descriptor={descriptors.binet_stage} type="select" value={formData?.binet_stage} options={BINET_STAGE_OPTIONS} onChange={onChange} />
          <ClinicalField label="Tumor Burden" name="tumor_burden" descriptor={descriptors.tumor_burden} type="select" value={formData?.tumor_burden} options={TUMOR_BURDEN_OPTIONS} onChange={onChange} />
          <ClinicalField label="Disease Activity" name="disease_activity" descriptor={descriptors.disease_activity} type="select" value={formData?.disease_activity} options={DISEASE_ACTIVITY_OPTIONS} onChange={onChange} />
          <ClinicalField label="Richter Transformation" name="richter_transformation" descriptor={descriptors.richter_transformation} type="select" value={formData?.richter_transformation} options={RICHTER_TRANSFORMATION_OPTIONS} onChange={onChange} />
          <div className="sm:col-span-2">
            <ClinicalField label="Protein Expressions" name="protein_expressions" descriptor={descriptors.protein_expressions} type="multiselect" value={formData?.protein_expressions} options={PROTEIN_EXPRESSION_OPTIONS} onChange={onChange} />
          </div>
        </div>
      </Section>

      <Section title="Laboratory Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Absolute Lymphocyte Count (×10⁹/L)" name="absolute_lymphocyte_count" descriptor={descriptors.absolute_lymphocyte_count} type="number" value={formData?.absolute_lymphocyte_count} onChange={onChange} />
          <ClinicalField label="Lymphocyte Doubling Time (months)" name="lymphocyte_doubling_time" descriptor={descriptors.lymphocyte_doubling_time} type="number" value={formData?.lymphocyte_doubling_time} onChange={onChange} />
          <ClinicalField label="Serum Beta-2 Microglobulin (mg/L)" name="serum_beta2_microglobulin_level" descriptor={descriptors.serum_beta2_microglobulin_level} type="number" value={formData?.serum_beta2_microglobulin_level} onChange={onChange} />
          <ClinicalField label="Clonal B-Lymphocyte Count" name="clonal_b_lymphocyte_count" descriptor={descriptors.clonal_b_lymphocyte_count} type="number" value={formData?.clonal_b_lymphocyte_count} onChange={onChange} />
          <ClinicalField label="Clonal Bone Marrow B-Lymphocytes (%)" name="clonal_bone_marrow_b_lymphocytes" descriptor={descriptors.clonal_bone_marrow_b_lymphocytes} type="number" value={formData?.clonal_bone_marrow_b_lymphocytes} onChange={onChange} />
          <ClinicalField label="QTcF Value (ms)" name="qtcf_value" descriptor={descriptors.qtcf_value} type="number" value={formData?.qtcf_value} onChange={onChange} />
          <ClinicalField label="Largest Lymph Node Size (cm)" name="largest_lymph_node_size" descriptor={descriptors.largest_lymph_node_size} type="number" value={formData?.largest_lymph_node_size} onChange={onChange} />
          <ClinicalField label="Spleen Size (cm)" name="spleen_size" descriptor={descriptors.spleen_size} type="number" value={formData?.spleen_size} onChange={onChange} />
        </div>
      </Section>

      <Section title="Clinical Findings">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Bone Marrow Involvement" name="bone_marrow_involvement" descriptor={descriptors.bone_marrow_involvement} type="boolean" value={formData?.bone_marrow_involvement} onChange={onChange} />
          <ClinicalField label="Measurable Disease (IWCLL)" name="measurable_disease_iwcll" descriptor={descriptors.measurable_disease_iwcll} type="boolean" value={formData?.measurable_disease_iwcll} onChange={onChange} />
          <ClinicalField label="Splenomegaly" name="splenomegaly" descriptor={descriptors.splenomegaly} type="boolean" value={formData?.splenomegaly} onChange={onChange} />
          <ClinicalField label="Hepatomegaly" name="hepatomegaly" descriptor={descriptors.hepatomegaly} type="boolean" value={formData?.hepatomegaly} onChange={onChange} />
          <ClinicalField label="Lymphadenopathy" name="lymphadenopathy" descriptor={descriptors.lymphadenopathy} type="boolean" value={formData?.lymphadenopathy} onChange={onChange} />
          <ClinicalField label="Autoimmune Cytopenias Refractory to Steroids" name="autoimmune_cytopenias_refractory_to_steroids" descriptor={descriptors.autoimmune_cytopenias_refractory_to_steroids} type="boolean" value={formData?.autoimmune_cytopenias_refractory_to_steroids} onChange={onChange} />
          <ClinicalField label="BTK Inhibitor Refractory" name="btk_inhibitor_refractory" descriptor={descriptors.btk_inhibitor_refractory} type="boolean" value={formData?.btk_inhibitor_refractory} onChange={onChange} />
          <ClinicalField label="BCL-2 Inhibitor Refractory" name="bcl2_inhibitor_refractory" descriptor={descriptors.bcl2_inhibitor_refractory} type="boolean" value={formData?.bcl2_inhibitor_refractory} onChange={onChange} />
        </div>
      </Section>
    </>
  );
}

function MCLSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  return (
    <>
      <Section title="Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Stage" name="stage" descriptor={descriptors.stage} type="select" value={formData?.stage} options={STAGE_OPTIONS} onChange={onChange} />
          <div className="sm:col-span-2">
            <ClinicalField label="Protein Expressions" name="protein_expressions" descriptor={descriptors.protein_expressions} type="multiselect" value={formData?.protein_expressions} options={PROTEIN_EXPRESSION_OPTIONS} onChange={onChange} />
          </div>
          <ClinicalField label="Ki-67 Proliferation Index (%)" name="ki67_proliferation_index" descriptor={descriptors.ki67_proliferation_index} type="number" value={formData?.ki67_proliferation_index} onChange={onChange} />
        </div>
      </Section>

      <Section title="Clinical Findings">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Lymphadenopathy" name="lymphadenopathy" descriptor={descriptors.lymphadenopathy} type="boolean" value={formData?.lymphadenopathy} onChange={onChange} />
          <ClinicalField label="Splenomegaly" name="splenomegaly" descriptor={descriptors.splenomegaly} type="boolean" value={formData?.splenomegaly} onChange={onChange} />
          <ClinicalField label="Spleen Size (cm)" name="spleen_size" descriptor={descriptors.spleen_size} type="number" value={formData?.spleen_size} onChange={onChange} />
          <ClinicalField label="Hepatomegaly" name="hepatomegaly" descriptor={descriptors.hepatomegaly} type="boolean" value={formData?.hepatomegaly} onChange={onChange} />
          <ClinicalField label="Largest Lymph Node Size (cm)" name="largest_lymph_node_size" descriptor={descriptors.largest_lymph_node_size} type="number" value={formData?.largest_lymph_node_size} onChange={onChange} />
        </div>
      </Section>
    </>
  );
}

function OtherSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  return (
    <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
      <ClinicalField label="Stage" name="stage" descriptor={descriptors.stage} type="select" value={formData?.stage} options={STAGE_OPTIONS} onChange={onChange} />
      <div className="sm:col-span-2">
        <p className="text-sm text-portal-text-secondary">
          Disease-specific fields are available for Breast Cancer, Follicular Lymphoma, Multiple Myeloma, CLL, and Mantle Cell Lymphoma.
        </p>
      </div>
    </div>
  );
}

/** Solid-tumor staging and assay-specific PD-L1 results for breast cancer. */
function StagingBiomarkersSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);

  const field = (label: string, name: string, type: 'text' | 'number') => (
    <ClinicalField
      label={label}
      name={name}
      type={type}
      value={formData?.[name]}
      descriptor={descriptors[name]}
      onChange={onChange}
    />
  );

  return (
    <Section title="Staging & Biomarkers">
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
        {field('Lymph Node Status', 'lymph_node_status', 'text')}
        {field('Metastasis Status', 'metastasis_status', 'text')}
        {field('PD-L1 Combined Positive Score', 'pd_l1_combined_positive_score', 'number')}
        {field('PD-L1 IC (%)', 'pd_l1_ic_percentage', 'number')}
        {field('PD-L1 Assay', 'pd_l1_assay', 'text')}
      </div>
    </Section>
  );
}

export default function DiseaseTab({ formData, onChange, diseaseType }: Props) {
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const { source: diseaseSource } = useVocabulary('disease', 'title');
  const diseaseSection = (() => {
    switch (diseaseType) {
      case 'breast':
        return <BreastCancerSection formData={formData} onChange={onChange} />;
      case 'lymphoma':
        return <LymphomaSection formData={formData} onChange={onChange} />;
      case 'myeloma':
        return <MyelomaSection formData={formData} onChange={onChange} />;
      case 'cll':
        return <CLLSection formData={formData} onChange={onChange} />;
      case 'mcl':
        return <MCLSection formData={formData} onChange={onChange} />;
      default:
        return <OtherSection formData={formData} onChange={onChange} />;
    }
  })();

  return (
    <>
      <Section title="Diagnosis">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <ClinicalField label="Disease" name="disease" descriptor={descriptors.disease} type="select" value={formData?.disease} options={DISEASE_OPTIONS} onChange={onChange} vocabSource={diseaseSource} />
        </div>
      </Section>
      {diseaseSection}
      {diseaseType === 'breast' && <StagingBiomarkersSection formData={formData} onChange={onChange} />}
    </>
  );
}
