import api from '@/api/axios';

export interface MappingStats {
  field_mappings: { total: number; approved: number; proposed: number; unmapped: number };
  code_mappings: { total: number; approved: number; proposed: number };
  therapy: { regimens: number; components: number; classes: number; disease_links: number };
}

export interface TherapyRegimenItem {
  code: string;
  title: string;
  concept_id?: number | null;
  components?: TherapyComponentItem[];
}

export interface TherapyComponentItem {
  code: string;
  title: string;
  concept_id?: number | null;
  classes?: TherapyClassItem[];
}

export interface TherapyClassItem {
  code: string;
  title: string;
  concept_id?: number | null;
}

export interface DiseaseTherapyRegimenItem {
  id: number;
  disease_code: string;
  disease_title?: string;
  round_code: string;
  round_title?: string;
  regimen_code: string;
  regimen_title?: string;
}

export async function fetchMappingStats(): Promise<MappingStats> {
  const resp = await api.get('/v1/mapping-stats/');
  return resp.data as MappingStats;
}

// --- Regimens ---

export async function fetchRegimens(search?: string): Promise<TherapyRegimenItem[]> {
  const params: Record<string, string> = {};
  if (search) params.search = search;
  const resp = await api.get('/v1/therapy-regimens/', { params });
  return resp.data as TherapyRegimenItem[];
}

export async function fetchRegimenDetail(code: string): Promise<TherapyRegimenItem> {
  const resp = await api.get(`/v1/therapy-regimens/${code}/`);
  return resp.data as TherapyRegimenItem;
}

export async function createRegimen(data: { code: string; title: string; concept_id?: number | null }): Promise<TherapyRegimenItem> {
  const resp = await api.post('/v1/therapy-regimens/', data);
  return resp.data as TherapyRegimenItem;
}

export async function updateRegimen(code: string, data: { title?: string; concept_id?: number | null }): Promise<TherapyRegimenItem> {
  const resp = await api.patch(`/v1/therapy-regimens/${code}/`, data);
  return resp.data as TherapyRegimenItem;
}

export async function deleteRegimen(code: string): Promise<void> {
  await api.delete(`/v1/therapy-regimens/${code}/`);
}

// --- Regimen Components ---

export async function addRegimenComponent(regimenCode: string, componentCode: string): Promise<void> {
  await api.post(`/v1/therapy-regimens/${regimenCode}/components/`, { component_code: componentCode });
}

export async function removeRegimenComponent(regimenCode: string, componentCode: string): Promise<void> {
  await api.delete(`/v1/therapy-regimens/${regimenCode}/components/${componentCode}/`);
}

// --- Components ---

export async function fetchComponents(search?: string): Promise<TherapyComponentItem[]> {
  const params: Record<string, string> = {};
  if (search) params.search = search;
  const resp = await api.get('/v1/therapy-components/', { params });
  return resp.data as TherapyComponentItem[];
}

export async function createComponent(data: { code: string; title: string; concept_id?: number | null }): Promise<TherapyComponentItem> {
  const resp = await api.post('/v1/therapy-components/', data);
  return resp.data as TherapyComponentItem;
}

// --- Component Classes ---

export async function addComponentClass(componentCode: string, classCode: string): Promise<void> {
  await api.post(`/v1/therapy-components/${componentCode}/classes/`, { class_code: classCode });
}

export async function removeComponentClass(componentCode: string, classCode: string): Promise<void> {
  await api.delete(`/v1/therapy-components/${componentCode}/classes/${classCode}/`);
}

// --- Classes ---

export async function fetchClasses(search?: string): Promise<TherapyClassItem[]> {
  const params: Record<string, string> = {};
  if (search) params.search = search;
  const resp = await api.get('/v1/therapy-classes/', { params });
  return resp.data as TherapyClassItem[];
}

export async function createClass(data: { code: string; title: string; concept_id?: number | null }): Promise<TherapyClassItem> {
  const resp = await api.post('/v1/therapy-classes/', data);
  return resp.data as TherapyClassItem;
}

// --- Disease-Therapy-Regimen ---

export async function fetchDiseaseTherapyRegimens(params?: { disease?: string; round?: string }): Promise<DiseaseTherapyRegimenItem[]> {
  const resp = await api.get('/v1/disease-therapy-regimens/', { params });
  return resp.data as DiseaseTherapyRegimenItem[];
}

export async function addDiseaseTherapyRegimen(data: {
  disease_code: string;
  round_code: string;
  regimen_code: string;
}): Promise<DiseaseTherapyRegimenItem> {
  const resp = await api.post('/v1/disease-therapy-regimens/', data);
  return resp.data as DiseaseTherapyRegimenItem;
}

export async function removeDiseaseTherapyRegimen(id: number): Promise<void> {
  await api.delete(`/v1/disease-therapy-regimens/${id}/`);
}
