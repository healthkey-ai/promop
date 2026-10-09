import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from './axios';
import { clearLegacyOAuthTokens } from '@/utils/oauth';

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  document.cookie = 'csrftoken=test-csrf; path=/';
  window.history.replaceState({}, '', '/login');
});

describe('session-only API authentication', () => {
  it('removes legacy credentials without clearing unrelated application data', () => {
    for (const storage of [sessionStorage, localStorage]) {
      for (const key of ['access_token', 'refresh_token', 'pkce_code_verifier', 'pkce_state']) {
        storage.setItem(key, 'legacy-value');
      }
      storage.setItem('theme', 'dark');
    }
    clearLegacyOAuthTokens();
    for (const storage of [sessionStorage, localStorage]) {
      expect(storage.length).toBe(1);
      expect(storage.getItem('theme')).toBe('dark');
    }
  });

  it('sends cookies and CSRF protection without using a stored bearer token', async () => {
    sessionStorage.setItem('access_token', 'old-access');
    sessionStorage.setItem('refresh_token', 'old-refresh');
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.withCredentials).toBe(true);
      expect(config.headers.Authorization).toBeUndefined();
      expect(config.headers['X-CSRFToken']).toBe('test-csrf');
      return { data: {}, status: 200, statusText: 'OK', headers: new AxiosHeaders(), config };
    });
    await api.post('/auth/login/', {}, { adapter });
    expect(adapter).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem('access_token')).toBeNull();
    expect(sessionStorage.getItem('refresh_token')).toBeNull();
  });

  it('rejects simultaneous 401s without refreshing, retrying, or leaving requests pending', async () => {
    sessionStorage.setItem('refresh_token', 'old-refresh');
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
        data: {}, status: 401, statusText: 'Unauthorized', headers: new AxiosHeaders(), config,
      });
    });
    const results = await Promise.allSettled([
      api.get('/user/', { adapter }), api.get('/patients/', { adapter }),
    ]);
    expect(results.map(result => result.status)).toEqual(['rejected', 'rejected']);
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(sessionStorage.getItem('refresh_token')).toBeNull();
  });
});
