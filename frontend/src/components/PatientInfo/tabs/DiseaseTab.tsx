import { useVocabulary } from '@/hooks/useVocabulary';
import { Button } from '@/components/shadcn/button';
import Field from '../Field';
import Section from '../Section';
import SelectControl from '../controls/SelectControl';
import { stringsToOptions } from '../utils';
import {
  STAGE_OPTIONS, HISTOLOGIC_TYPE_OPTIONS,
  MENOPAUSAL_OPTIONS, TUMOR_STAGE_OPTIONS, NODES_STAGE_OPTIONS,
  STAGING_MODALITIES_OPTIONS, DISTANT_METASTASIS_STAGE_OPTIONS,
  YES_NO_OPTIONS, ER_OPTIONS, PR_OPTIONS, HER2_OPTIONS, HR_OPTIONS, HRD_OPTIONS,
  DISEASE_OPTIONS,
  FLIPI_RISK_OPTIONS, GELF_OPTIONS, FL_TUMOR_GRADE_OPTIONS,
  ISS_STAGE_OPTIONS, MM_PROGRESSION_OPTIONS, STEM_CELL_TRANSPLANT_OPTIONS,
  MRD_STATUS_OPTIONS, CYTOGENETIC_RISK_OPTIONS,
  BINET_STAGE_OPTIONS, TUMOR_BURDEN_OPTIONS, DISEASE_ACTIVITY_OPTIONS,
  RICHTER_TRANSFORMATION_OPTIONS, PROTEIN_EXPRESSION_OPTIONS,
  GENE_OPTIONS, MUTATION_OPTIONS, ORIGIN_OPTIONS, INTERPRETATION_OPTIONS,
} from '../patientConstants';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  onMutationAdd: () => void;
  onMutationRemove: (index: number) => void;
  onMutationChange: (index: number, field: string, value: string) => void;
  diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'other';
}

function BreastCancerSection({ formData, onChange, onMutationAdd, onMutationRemove, onMutationChange }: Omit<Props, 'diseaseType'>) {
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
  const mutations = (formData?.genetic_mutations || []) as { gene: string; mutation: string; origin: string; interpretation: string }[];

  return (
    <>
      <Section title="Tumor Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Histologic Type" name="histologic_type" type="select" value={formData?.histologic_type} options={histOptions} onChange={onChange} vocabSource={histologicSource} />
          </div>
          <Field label="Menopausal Status" name="menopausal_status" type="select" value={formData?.menopausal_status} options={MENOPAUSAL_OPTIONS} onChange={onChange} />
          <Field label="Tumor Stage" name="tumor_stage" type="select" value={formData?.tumor_stage} options={TUMOR_STAGE_OPTIONS} onChange={onChange} vocabSource={tumorStageSource} />
          <Field label="Nodes Stage" name="nodes_stage" type="select" value={formData?.nodes_stage} options={NODES_STAGE_OPTIONS} onChange={onChange} vocabSource={nodesStageSource} />
          <Field label="Staging Modalities" name="staging_modalities" type="select" value={formData?.staging_modalities} options={STAGING_MODALITIES_OPTIONS} onChange={onChange} vocabSource={stagingModalitySource} />
          <Field label="Distant Metastasis Stage" name="distant_metastasis_stage" type="select" value={formData?.distant_metastasis_stage} options={DISTANT_METASTASIS_STAGE_OPTIONS} onChange={onChange} vocabSource={distantMetSource} />
          <Field label="Bone-Only Metastasis" name="bone_only_metastasis_status" type="boolean" value={formData?.bone_only_metastasis_status} onChange={onChange} />
          <Field label="Measurable Disease by RECIST" name="measurable_disease_by_recist_status" type="boolean" value={formData?.measurable_disease_by_recist_status} onChange={onChange} />
        </div>
      </Section>

      <Section title="Receptor Status">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Estrogen Receptor (ER) Status" name="estrogen_receptor_status" type="select" value={formData?.estrogen_receptor_status} options={ER_OPTIONS} onChange={onChange} vocabSource={erSource} />
          <Field label="Progesterone Receptor (PR) Status" name="progesterone_receptor_status" type="select" value={formData?.progesterone_receptor_status} options={PR_OPTIONS} onChange={onChange} vocabSource={prSource} />
          <Field label="HER2 Status" name="her2_status" type="select" value={formData?.her2_status} options={HER2_OPTIONS} onChange={onChange} vocabSource={her2Source} />
          <Field label="HR Status" name="hr_status" type="select" value={formData?.hr_status} options={HR_OPTIONS} onChange={onChange} vocabSource={hrSource} />
          <Field label="HRD Status" name="hrd_status" type="select" value={formData?.hrd_status} options={HRD_OPTIONS} onChange={onChange} vocabSource={hrdSource} />
          <Field label="Androgen Receptor Status" name="androgen_receptor_status" type="select" value={formData?.androgen_receptor_status} options={ER_OPTIONS} onChange={onChange} />
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
          <Field label="Ki-67 Proliferation Index (%)" name="ki67_proliferation_index" type="number" value={formData?.ki67_proliferation_index} onChange={onChange} />
          <Field label="PD-L1 Status (%)" name="pd_l1_tumor_cells" type="number" value={formData?.pd_l1_tumor_cells} onChange={onChange} />
          <Field label="Oncotype DX Score" name="oncotype_dx_score" type="number" value={formData?.oncotype_dx_score} onChange={onChange} />
        </div>
      </Section>

      <Section title="Test Information">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Test Methodology" name="test_methodology" type="text" value={formData?.test_methodology} onChange={onChange} />
          </div>
          <Field label="Test Date" name="test_date" type="date" value={formData?.test_date} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Test Specimen Type" name="test_specimen_type" type="text" value={formData?.test_specimen_type} onChange={onChange} />
          </div>
          <div className="sm:col-span-2">
            <Field label="Report Interpretation" name="report_interpretation" type="text" value={formData?.report_interpretation} onChange={onChange} />
          </div>
        </div>
      </Section>

      <Section title="Genetic Mutations">
        <div className="flex items-center justify-between mb-4">
          <p className="text-sm text-portal-text-secondary">{mutations.length} mutation(s) identified</p>
          <Button variant="outline" size="sm" onClick={onMutationAdd}>Add Mutation</Button>
        </div>

        {mutations.map((mutation: { gene: string; mutation: string; origin: string; interpretation: string }, index: number) => (
          <div key={index} className="mb-4 p-4 border border-portal-border rounded-md">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm font-medium text-portal-text-primary">Mutation {index + 1}</span>
              <Button variant="ghost" size="sm" onClick={() => onMutationRemove(index)}
                className="text-red-600 hover:text-red-700 hover:bg-red-50">
                Remove
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-portal-text-primary">Gene</label>
                <SelectControl
                  value={mutation.gene || ''}
                  options={stringsToOptions(GENE_OPTIONS)}
                  treatEmptyOptionAsUnknown={false}
                  onChange={(v) => onMutationChange(index, 'gene', String(v ?? ''))}
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-portal-text-primary">Mutation</label>
                <SelectControl
                  value={mutation.mutation || ''}
                  options={mutation.gene ? stringsToOptions(MUTATION_OPTIONS[mutation.gene] || []) : []}
                  disabled={!mutation.gene}
                  treatEmptyOptionAsUnknown={false}
                  onChange={(v) => onMutationChange(index, 'mutation', String(v ?? ''))}
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-portal-text-primary">Origin</label>
                <SelectControl
                  value={mutation.origin || ''}
                  options={stringsToOptions(ORIGIN_OPTIONS)}
                  treatEmptyOptionAsUnknown={false}
                  onChange={(v) => onMutationChange(index, 'origin', String(v ?? ''))}
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-portal-text-primary">Interpretation</label>
                <SelectControl
                  value={mutation.interpretation || ''}
                  options={stringsToOptions(INTERPRETATION_OPTIONS)}
                  treatEmptyOptionAsUnknown={false}
                  onChange={(v) => onMutationChange(index, 'interpretation', String(v ?? ''))}
                />
              </div>
            </div>
          </div>
        ))}

        {mutations.length === 0 && (
          <p className="text-sm text-portal-text-secondary italic text-center py-4">
            No genetic mutations identified. Click "Add Mutation" to add one.
          </p>
        )}
      </Section>
    </>
  );
}

function LymphomaSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  const { source: gelfSource }    = useVocabulary('gelf-criteria', 'title');
  const { source: flipiSource }   = useVocabulary('flipi-score', 'code');
  const { source: flGradeSource } = useVocabulary('follicular-lymphoma-grade', 'title');
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');
  const histOptions = histologicOptions.length ? histologicOptions.map((o: { value: string }) => o.value) : HISTOLOGIC_TYPE_OPTIONS;

  return (
    <>
      <Section title="Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Histologic Subtype" name="histologic_type" type="select" value={formData?.histologic_type} options={histOptions} onChange={onChange} vocabSource={histologicSource} />
          </div>
          <Field label="Ann Arbor Stage" name="stage" type="select" value={formData?.stage} options={STAGE_OPTIONS} onChange={onChange} />
          <Field label="Tumor Grade" name="tumor_grade" type="select" value={formData?.tumor_grade} options={FL_TUMOR_GRADE_OPTIONS} onChange={onChange} vocabSource={flGradeSource} />
          <Field label="GELF Criteria" name="gelf_criteria_status" type="select" value={formData?.gelf_criteria_status} options={GELF_OPTIONS} onChange={onChange} vocabSource={gelfSource} />
          <Field label="FLIPI Score" name="flipi_score" type="number" value={formData?.flipi_score} onChange={onChange} />
          <Field label="FLIPI Risk Category" name="flipi_risk_category" type="select" value={formData?.flipi_risk_category} options={FLIPI_RISK_OPTIONS} onChange={onChange} vocabSource={flipiSource} />
          <Field label="Bulky Disease" name="bulky_disease" type="select" value={formData?.bulky_disease} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="B Symptoms" name="b_symptoms" type="select" value={formData?.b_symptoms} options={YES_NO_OPTIONS} onChange={onChange} />
        </div>
      </Section>

      <Section title="Laboratory Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="LDH Level (U/L)" name="ldh_level" type="number" value={formData?.ldh_level} onChange={onChange} />
          <Field label="Beta-2 Microglobulin (mg/L)" name="beta2_microglobulin" type="number" value={formData?.beta2_microglobulin} onChange={onChange} />
          <Field label="Bone Marrow Involvement" name="bone_marrow_involvement" type="select" value={formData?.bone_marrow_involvement} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="Clonal Bone Marrow B Lymphocytes (%)" name="clonal_bone_marrow_b_lymphocytes" type="number" value={formData?.clonal_bone_marrow_b_lymphocytes} onChange={onChange} />
          <Field label="Number of Nodal Sites" name="number_of_nodal_sites" type="number" value={formData?.number_of_nodal_sites} onChange={onChange} />
        </div>
      </Section>
    </>
  );
}

function MyelomaSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  const { source: progressionSource } = useVocabulary('disease-progression', 'title');
  const { options: sctEligibilityOptions, source: sctEligibilitySource } = useVocabulary('sct-eligibility', 'title');

  return (
    <>
      <Section title="Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Myeloma Type" name="myeloma_type" type="text" value={formData?.myeloma_type} onChange={onChange} />
          <Field label="ISS Stage" name="stage" type="select" value={formData?.stage} options={ISS_STAGE_OPTIONS} onChange={onChange} />
          <Field label="R-ISS Stage" name="r_iss_stage" type="select" value={formData?.r_iss_stage} options={ISS_STAGE_OPTIONS} onChange={onChange} />
          <Field label="Durie-Salmon Stage" name="durie_salmon_stage" type="text" value={formData?.durie_salmon_stage} onChange={onChange} />
          <Field label="Progression Status" name="progression" type="select" value={formData?.progression} options={MM_PROGRESSION_OPTIONS} onChange={onChange} vocabSource={progressionSource} />
          <Field label="Measurable Disease (IMWG)" name="measurable_disease_imwg" type="boolean" value={formData?.measurable_disease_imwg} onChange={onChange} />
          <Field label="MRD Status" name="mrd_status" type="select" value={formData?.mrd_status} options={MRD_STATUS_OPTIONS} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Prior SCT Type" name="stem_cell_transplant_history" type="multiselect" value={formData?.stem_cell_transplant_history} options={STEM_CELL_TRANSPLANT_OPTIONS} onChange={onChange} />
          </div>
          <Field label="SCT Date" name="sct_date" type="date" value={formData?.sct_date} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="SCT Eligibility" name="sct_eligibility" type="multiselect" value={formData?.sct_eligibility} options={sctEligibilityOptions.length ? sctEligibilityOptions.map((o: { value: string }) => o.value) : []} onChange={onChange} vocabSource={sctEligibilitySource} />
          </div>
        </div>
      </Section>

      <Section title="Myeloma Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="M-Protein Type" name="m_protein_type" type="text" value={formData?.m_protein_type} onChange={onChange} />
          <Field label="Serum M-Protein (g/dL)" name="serum_m_protein" type="number" value={formData?.serum_m_protein} onChange={onChange} />
          <Field label="Urine M-Protein (mg/24h)" name="urine_m_protein" type="number" value={formData?.urine_m_protein} onChange={onChange} />
          <Field label="Free Light Chain Ratio" name="free_light_chain_ratio" type="number" value={formData?.free_light_chain_ratio} onChange={onChange} />
          <Field label="Beta-2 Microglobulin (mg/L)" name="beta2_microglobulin" type="number" value={formData?.beta2_microglobulin} onChange={onChange} />
          <Field label="LDH Level (U/L)" name="ldh_level" type="number" value={formData?.ldh_level} onChange={onChange} />
        </div>
      </Section>

      <Section title="Complications">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Bone Lesions" name="bone_lesions" type="select" value={formData?.bone_lesions} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="Hypercalcemia" name="hypercalcemia" type="select" value={formData?.hypercalcemia} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="Renal Impairment" name="renal_impairment" type="select" value={formData?.renal_impairment} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="Anemia" name="anemia" type="select" value={formData?.anemia} options={YES_NO_OPTIONS} onChange={onChange} />
          <Field label="Plasma Cell Percentage (%)" name="plasma_cell_percentage" type="number" value={formData?.plasma_cell_percentage} onChange={onChange} />
        </div>
      </Section>

      <Section title="Cytogenetics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Cytogenetic Risk" name="cytogenetic_risk" type="select" value={formData?.cytogenetic_risk} options={CYTOGENETIC_RISK_OPTIONS} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Cytogenetic Abnormalities" name="cytogenetic_abnormalities" type="text" value={formData?.cytogenetic_abnormalities} onChange={onChange} />
          </div>
          <div className="sm:col-span-2">
            <Field label="Genetic Mutations" name="genetic_mutations" type="text" value={formData?.genetic_mutations} onChange={onChange} />
          </div>
        </div>
      </Section>
    </>
  );
}

function CLLSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  return (
    <>
      <Section title="CLL Disease Characteristics">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Binet Stage" name="binet_stage" type="select" value={formData?.binet_stage} options={BINET_STAGE_OPTIONS} onChange={onChange} />
          <Field label="Tumor Burden" name="tumor_burden" type="select" value={formData?.tumor_burden} options={TUMOR_BURDEN_OPTIONS} onChange={onChange} />
          <Field label="Disease Activity" name="disease_activity" type="select" value={formData?.disease_activity} options={DISEASE_ACTIVITY_OPTIONS} onChange={onChange} />
          <Field label="Richter Transformation" name="richter_transformation" type="select" value={formData?.richter_transformation} options={RICHTER_TRANSFORMATION_OPTIONS} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Protein Expressions" name="protein_expressions" type="multiselect" value={formData?.protein_expressions} options={PROTEIN_EXPRESSION_OPTIONS} onChange={onChange} />
          </div>
        </div>
      </Section>

      <Section title="Laboratory Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Absolute Lymphocyte Count (×10⁹/L)" name="absolute_lymphocyte_count" type="number" value={formData?.absolute_lymphocyte_count} onChange={onChange} />
          <Field label="Lymphocyte Doubling Time (months)" name="lymphocyte_doubling_time" type="number" value={formData?.lymphocyte_doubling_time} onChange={onChange} />
          <Field label="Serum Beta-2 Microglobulin (mg/L)" name="serum_beta2_microglobulin_level" type="number" value={formData?.serum_beta2_microglobulin_level} onChange={onChange} />
          <Field label="Clonal B-Lymphocyte Count" name="clonal_b_lymphocyte_count" type="number" value={formData?.clonal_b_lymphocyte_count} onChange={onChange} />
          <Field label="Clonal Bone Marrow B-Lymphocytes (%)" name="clonal_bone_marrow_b_lymphocytes" type="number" value={formData?.clonal_bone_marrow_b_lymphocytes} onChange={onChange} />
          <Field label="QTcF Value (ms)" name="qtcf_value" type="number" value={formData?.qtcf_value} onChange={onChange} />
          <Field label="Largest Lymph Node Size (cm)" name="largest_lymph_node_size" type="number" value={formData?.largest_lymph_node_size} onChange={onChange} />
          <Field label="Spleen Size (cm)" name="spleen_size" type="number" value={formData?.spleen_size} onChange={onChange} />
        </div>
      </Section>

      <Section title="Clinical Findings">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="TP53 Disruption" name="tp53_disruption" type="boolean" value={formData?.tp53_disruption} onChange={onChange} />
          <Field label="Bone Marrow Involvement" name="bone_marrow_involvement" type="boolean" value={formData?.bone_marrow_involvement} onChange={onChange} />
          <Field label="Measurable Disease (IWCLL)" name="measurable_disease_iwcll" type="boolean" value={formData?.measurable_disease_iwcll} onChange={onChange} />
          <Field label="Splenomegaly" name="splenomegaly" type="boolean" value={formData?.splenomegaly} onChange={onChange} />
          <Field label="Hepatomegaly" name="hepatomegaly" type="boolean" value={formData?.hepatomegaly} onChange={onChange} />
          <Field label="Lymphadenopathy" name="lymphadenopathy" type="boolean" value={formData?.lymphadenopathy} onChange={onChange} />
          <Field label="Autoimmune Cytopenias Refractory to Steroids" name="autoimmune_cytopenias_refractory_to_steroids" type="boolean" value={formData?.autoimmune_cytopenias_refractory_to_steroids} onChange={onChange} />
          <Field label="BTK Inhibitor Refractory" name="btk_inhibitor_refractory" type="boolean" value={formData?.btk_inhibitor_refractory} onChange={onChange} />
          <Field label="BCL-2 Inhibitor Refractory" name="bcl2_inhibitor_refractory" type="boolean" value={formData?.bcl2_inhibitor_refractory} onChange={onChange} />
        </div>
      </Section>
    </>
  );
}

function OtherSection({ formData, onChange }: Pick<Props, 'formData' | 'onChange'>) {
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');
  const histOptions = histologicOptions.length ? histologicOptions.map((o: { value: string }) => o.value) : HISTOLOGIC_TYPE_OPTIONS;

  return (
    <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
      <Field label="Disease" name="disease" type="select" value={formData?.disease} options={DISEASE_OPTIONS} onChange={onChange} />
      <Field label="Stage" name="stage" type="select" value={formData?.stage} options={STAGE_OPTIONS} onChange={onChange} />
      <div className="sm:col-span-2">
        <Field label="Histologic Type" name="histologic_type" type="select" value={formData?.histologic_type} options={histOptions} onChange={onChange} vocabSource={histologicSource} />
      </div>
      <div className="sm:col-span-2">
        <p className="text-sm text-portal-text-secondary">
          Disease-specific fields are available for Breast Cancer, Follicular Lymphoma, Multiple Myeloma, and CLL.
        </p>
      </div>
    </div>
  );
}

export default function DiseaseTab({ formData, onChange, onMutationAdd, onMutationRemove, onMutationChange, diseaseType }: Props) {
  switch (diseaseType) {
    case 'breast':
      return <BreastCancerSection formData={formData} onChange={onChange} onMutationAdd={onMutationAdd} onMutationRemove={onMutationRemove} onMutationChange={onMutationChange} />;
    case 'lymphoma':
      return <LymphomaSection formData={formData} onChange={onChange} />;
    case 'myeloma':
      return <MyelomaSection formData={formData} onChange={onChange} />;
    case 'cll':
      return <CLLSection formData={formData} onChange={onChange} />;
    default:
      return <OtherSection formData={formData} onChange={onChange} />;
  }
}
