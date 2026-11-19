/**
 * Signs the round-trip suite in as a user of the local backend (:8011, promop_dev).
 *
 * The session and CSRF values belong to a throwaway local login, so they are read
 * from the environment instead of being committed:
 *
 *   ITEST_SESSION_ID=<sessionid cookie> ITEST_CSRF_TOKEN=<csrftoken cookie> \
 *     npx vitest run --config vitest.itest.config.ts
 *
 * Log in to the local backend once and copy the two cookies from the browser.
 */
export function signInForItest(): void {
  const cookies: Record<string, string | undefined> = {
    sessionid: process.env.ITEST_SESSION_ID,
    csrftoken: process.env.ITEST_CSRF_TOKEN,
  };
  for (const [name, value] of Object.entries(cookies)) {
    if (!value) {
      throw new Error(`Set ITEST_SESSION_ID and ITEST_CSRF_TOKEN to run the round-trip suite (${name} missing).`);
    }
    document.cookie = `${name}=${value}`;
  }
}
