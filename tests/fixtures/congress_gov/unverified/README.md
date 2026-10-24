# Unverified shape fixtures

These files were **not** recorded from the live API. The shared `DEMO_KEY` ran out
(10 requests per hour, then `Retry-After` of about 5 hours) before these endpoints could
be recorded, so they follow the response shapes documented at
https://github.com/LibraryOfCongress/api.congress.gov. All values (bill numbers in the
9000s, titles, dates) are invented for tests. They do not describe real legislation or
real sponsorships.

Replace each one with a recorded response (API key removed) when you have a key:

| File | Endpoint |
| --- | --- |
| `sponsored_D000197_0.json` | `GET /member/D000197/sponsored-legislation` |
| `cosponsored_D000197_0.json` | `GET /member/D000197/cosponsored-legislation` |
| `bill_cosponsors_119_hr_9002_0.json` | `GET /bill/119/hr/9002/cosponsors` |
| `bill_text_119_hr_9003.json` | `GET /bill/119/hr/9003/text` |
| `bill_text_version_119_hr_9003.htm` | the "Formatted Text" URL listed by the text endpoint |

Things to confirm when recording: whether cosponsored-legislation items carry a
cosponsorship date (the code assumes not, and fetches `/bill/.../cosponsors` for
`sponsorshipDate`), and the `textVersions[].formats[].type` label for HTML text.
