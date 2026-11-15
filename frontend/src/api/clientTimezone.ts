/** The browser's IANA zone, used only on writes that validate calendar dates. */
export function clientTimezoneHeaders() {
  return {
    'X-Client-Timezone': Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  };
}
