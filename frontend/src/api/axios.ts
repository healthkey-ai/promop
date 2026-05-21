import axios from 'axios';
import { ACCESS_TOKEN_KEY, refreshAccessToken, clearTokens } from '@/utils/oauth';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
});

api.interceptors.request.use(
  (config) => {
    const accessToken = sessionStorage.getItem(ACCESS_TOKEN_KEY);
    if (accessToken) {
      config.headers['Authorization'] = `Bearer ${accessToken}`;
    } else {
      const csrfToken = document.cookie
        .split('; ')
        .find(row => row.startsWith('csrftoken='))
        ?.split('=')[1];
      if (csrfToken) {
        config.headers['X-CSRFToken'] = csrfToken;
      }
    }
    return config;
  },
  (error) => Promise.reject(error)
);

let isRefreshing = false;
let failedQueue: Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void }> = [];

function processQueue(error: unknown, token: string | null) {
  failedQueue.forEach(({ resolve, reject }) => {
    if (error) reject(error);
    else resolve(token);
  });
  failedQueue = [];
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        }).then((token) => {
          originalRequest.headers['Authorization'] = `Bearer ${token}`;
          return api(originalRequest);
        });
      }

      originalRequest._retry = true;
      isRefreshing = true;

      try {
        const tokens = await refreshAccessToken();
        if (tokens) {
          processQueue(null, tokens.access_token);
          originalRequest.headers['Authorization'] = `Bearer ${tokens.access_token}`;
          return api(originalRequest);
        }
      } catch (refreshError) {
        processQueue(refreshError, null);
      } finally {
        isRefreshing = false;
      }

      clearTokens();
      if (!window.location.pathname.includes('/login')) {
        window.location.href = '/login';
      }
    }

    return Promise.reject(error);
  }
);

export default api;

export const omopApi = {
  conditions: {
    list: (personId: number) => api.get(`/conditions/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/conditions/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/conditions/${id}/`, data),
    delete: (id: number) => api.delete(`/conditions/${id}/`),
  },
  drugExposures: {
    list: (personId: number) => api.get(`/drug-exposures/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/drug-exposures/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/drug-exposures/${id}/`, data),
    delete: (id: number) => api.delete(`/drug-exposures/${id}/`),
  },
  measurements: {
    list: (personId: number) => api.get(`/measurements/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/measurements/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/measurements/${id}/`, data),
    delete: (id: number) => api.delete(`/measurements/${id}/`),
  },
  observations: {
    list: (personId: number) => api.get(`/observations/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/observations/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/observations/${id}/`, data),
  },
  procedures: {
    list: (personId: number) => api.get(`/procedures/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/procedures/', data),
  },
  episodes: {
    list: (personId: number) => api.get(`/episodes/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/episodes/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/episodes/${id}/`, data),
  },
  patientConditions: {
    list: (personId: number) => api.get(`/patient-conditions/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/patient-conditions/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/patient-conditions/${id}/`, data),
    delete: (id: number) => api.delete(`/patient-conditions/${id}/`),
  },
  therapyLines: {
    list: (personId: number) => api.get(`/therapy-lines/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/therapy-lines/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/therapy-lines/${id}/`, data),
    delete: (id: number) => api.delete(`/therapy-lines/${id}/`),
  },
  medications: {
    list: (therapyLineId: number) => api.get(`/medications/?therapy_line_id=${therapyLineId}`),
    create: (data: Record<string, unknown>) => api.post('/medications/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/medications/${id}/`, data),
    delete: (id: number) => api.delete(`/medications/${id}/`),
  },
  patientProcedures: {
    list: (personId: number) => api.get(`/patient-procedures/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/patient-procedures/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/patient-procedures/${id}/`, data),
    delete: (id: number) => api.delete(`/patient-procedures/${id}/`),
  },
  documents: {
    list: (personId: number) => api.get(`/documents/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/documents/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/documents/${id}/`, data),
    delete: (id: number) => api.delete(`/documents/${id}/`),
  },
  sideEffects: {
    list: (personId: number) => api.get(`/side-effects/?person_id=${personId}`),
    create: (data: Record<string, unknown>) => api.post('/side-effects/', data),
    update: (id: number, data: Record<string, unknown>) => api.patch(`/side-effects/${id}/`, data),
  },
  trialMatches: {
    list: (personId: number) => api.get(`/trial-matches/?person_id=${personId}`),
  },
};
