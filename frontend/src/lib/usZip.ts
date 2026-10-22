// The ZIP autofill looks up api.zippopotam.us/us/, which knows only US ZIP codes — and
// Germany, France, Spain and Italy use 5-digit postcodes too.
//
// The form stores "United States", but the record can also carry what an import or another
// client wrote: "US" from FHIR/Synthea, "United States of America" from CancerBot.
const US_SPELLINGS = new Set(["us", "usa", "united states", "united states of america"]);

/** True unless the country is set to something that is not the US. */
export function mayBeUnitedStates(country: unknown): boolean {
  if (country == null || String(country).trim() === "") return true;
  return US_SPELLINGS.has(String(country).trim().toLowerCase());
}
