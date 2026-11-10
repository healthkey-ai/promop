/** Whether a store publish carries a save error the host has not been told.
 *
 *  The store republishes `errors` as a new object on every save (it drops the
 *  answered stale-date errors), so identity says nothing: an old error that
 *  merely survived a successful save of another field must not fire again.
 *  An edit clears its field's error before the retry, so a retry that fails
 *  with the same message is still new here. */
export function hasNewSaveError(
  previous: Record<string, string>,
  next: Record<string, string>,
): boolean {
  return Object.entries(next).some(([field, message]) => !!message && previous[field] !== message);
}
