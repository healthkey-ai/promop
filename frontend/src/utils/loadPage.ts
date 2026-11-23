/**
 * Go to a page of this app with a full page load, not a client-side route
 * change. For the moment the signed-in account itself has just changed (it
 * gained a patient link, say): the app reads the account once, when it
 * loads, so only a load shows the new one (#2818).
 */
export function loadPage(url: string): void {
  window.location.assign(url);
}
