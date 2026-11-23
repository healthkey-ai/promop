# Assets

Files that something outside the repository links to. Renaming or moving one
breaks that link, so check who uses it first.

| File | What | Public URL |
|---|---|---|
| `promop-logo-300x300.png` | The PRomop logo as a 300x300 opaque PNG, for developer programs that ask for an application logo at a URL (Garmin's, for the [Garmin Connect sync](../wearable-omop-mapping.md#garmin-connect-sync)). | `https://raw.githubusercontent.com/healthkey-ai/promop/dev/docs/assets/promop-logo-300x300.png` |

The PNG is drawn from `frontend/src/assets/promop-logo.svg`: the same artwork,
scaled and centred on a square of the logo's own background colour (`#F5F0EA`).
If the SVG changes, render the PNG again.
