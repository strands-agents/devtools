# Goals: release-notes

- Base and head identified; PR-list provenance stated (release body or compare).
- Every PR categorized, proportionately (~3–8 Major Features, 0–5 Major Bug Fixes).
- Every Major Feature has an example validated by a behavioural test that actually ran (test code
  + output in the report); unvalidated ones carry the marker plus documented attempts with real
  errors; no feature dropped for failed validation.
- Format contract met (`### Name - [PR#N](link)` prose sections, fenced examples, `---` separators,
  no "Full Changelog" link); report has the three blocks in order.
- AWS resources followed the e2e-test boundary and were deleted; nothing published unless asked.
