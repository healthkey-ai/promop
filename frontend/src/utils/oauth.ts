const CLIENT_ID = import.meta.env.VITE_OAUTH_CLIENT_ID ?? 'ctomop-smart-app';
const REDIRECT_URI =
  import.meta.env.VITE_OAUTH_REDIRECT_URI ??
  `${window.location.origin}/auth/callback`;
const SCOPES = 'openid patient/*.read offline_access';

const CODE_VERIFIER_KEY = 'pkce_code_verifier';
const STATE_KEY = 'pkce_state';
export const ACCESS_TOKEN_KEY = 'access_token';
export const REFRESH_TOKEN_KEY = 'refresh_token';

function randomBytes(length: number): Uint8Array {
  const array = new Uint8Array(length);
  window.crypto.getRandomValues(array);
  return array;
}

function base64UrlEncode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

async function sha256(plain: string): Promise<ArrayBuffer> {
  const encoder = new TextEncoder();
  return window.crypto.subtle.digest('SHA-256', encoder.encode(plain));
}

async function generatePkce(): Promise<{ verifier: string; challenge: string }> {
  const verifierBytes = randomBytes(32);
  const verifier = base64UrlEncode(verifierBytes.buffer as ArrayBuffer);
  const challengeBuffer = await sha256(verifier);
  const challenge = base64UrlEncode(challengeBuffer);
  return { verifier, challenge };
}

function generateState(): string {
  return base64UrlEncode(randomBytes(16).buffer as ArrayBuffer);
}

export async function startPkceLogin(): Promise<void> {
  const { verifier, challenge } = await generatePkce();
  const state = generateState();

  sessionStorage.setItem(CODE_VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: SCOPES,
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });

  window.location.assign(`/o/authorize/?${params.toString()}`);
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token?: string;
  scope: string;
}

export async function exchangeCodeForToken(
  code: string,
  returnedState: string
): Promise<TokenResponse> {
  const storedState = sessionStorage.getItem(STATE_KEY);
  if (storedState && storedState !== returnedState) {
    throw new Error('OAuth state mismatch — possible CSRF attack');
  }

  const verifier = sessionStorage.getItem(CODE_VERIFIER_KEY);
  if (!verifier) {
    throw new Error('Missing PKCE code verifier');
  }

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: REDIRECT_URI,
    client_id: CLIENT_ID,
    code_verifier: verifier,
  });

  const resp = await fetch('/o/token/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Token exchange failed: ${err}`);
  }

  const tokens: TokenResponse = await resp.json();

  sessionStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
  if (tokens.refresh_token) {
    sessionStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
  }

  sessionStorage.removeItem(CODE_VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);

  return tokens;
}

export async function refreshAccessToken(): Promise<TokenResponse | null> {
  const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_KEY);
  if (!refreshToken) return null;

  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: CLIENT_ID,
  });

  const resp = await fetch('/o/token/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });

  if (!resp.ok) {
    clearTokens();
    return null;
  }

  const tokens: TokenResponse = await resp.json();
  sessionStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
  if (tokens.refresh_token) {
    sessionStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
  }
  return tokens;
}

export function clearTokens(): void {
  sessionStorage.removeItem(ACCESS_TOKEN_KEY);
  sessionStorage.removeItem(REFRESH_TOKEN_KEY);
  sessionStorage.removeItem(CODE_VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
}

export function hasAccessToken(): boolean {
  return !!sessionStorage.getItem(ACCESS_TOKEN_KEY);
}
