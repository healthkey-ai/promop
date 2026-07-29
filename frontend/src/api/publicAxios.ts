import axios from 'axios';

/**
 * Unauthenticated axios instance for public endpoints (login, signup, invite
 * acceptance). Unlike the main `api` instance, this does NOT attach Bearer
 * tokens or CSRF headers — it is intentionally credential-free.
 */
export const publicApi = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  headers: { 'Content-Type': 'application/json' },
});
