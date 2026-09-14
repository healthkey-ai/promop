// Older SPA builds persisted OAuth credentials. Session cookies now own login;
// clear both storage areas so an upgrade cannot reuse those credentials.
export function clearLegacyOAuthTokens(): void {
  for (const storage of [sessionStorage, localStorage]) {
    for (const key of ['access_token', 'refresh_token', 'pkce_code_verifier', 'pkce_state']) {
      storage.removeItem(key);
    }
  }
}
