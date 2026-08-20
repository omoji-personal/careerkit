# Fixture provenance

Every fixture in this directory is a trimmed, sanitized response from a public
job board. Fields not exercised by the adapter contract are removed; no field
shape is invented. The nine enterprise/HTML fixtures added on 2026-08-08 came
from these official public endpoints:

- `workday.json` — Salesforce Workday CXS, cached from
  `salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs`.
- `icims.html` — Wipfli's public iCIMS search page, cached from
  `careers-wipfli.icims.com/jobs/search`.
- `jobvite.html` — Feeding America's public Jobvite search page, cached from
  `jobs.jobvite.com/feedingamerica/search`.
- `paylocity.html` — North Carolina Advanced Energy Corporation's public
  Paylocity board at `recruiting.paylocity.com/recruiting/jobs/All/211e692d-c45a-4e3a-ae01-3e497af97929`.
- `phenom.html` — Phenom's own public board at
  `careers.phenom.com/global/en/search-results`.
- `eightfold.json` — Albemarle's public Eightfold API at
  `albemarle.eightfold.ai/api/apply/v2/jobs?domain=albemarle.com`.
- `workable.json` — Zaelab's public Workable widget, cached from
  `apply.workable.com/api/v1/widget/accounts/zaelab?details=true`.
- `teamtailor.json` — Teamtailor's own public JSON Feed at
  `career.teamtailor.com/jobs.json`.
- `oracle_orc.json` — the public Oracle Recruiting Cloud board at
  `ejov.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/`.
- `oracle_orc_detail.json` — a trimmed, employer-name-sanitized response from
  Oracle Candidate Experience's public requisition-detail resource.
- `pinpoint.json` — a trimmed, employer-name-sanitized response using fields
  observed on Pinpoint's documented public Sensiba and JED `postings.json`
  endpoints.
- `neogov.xml` — a trimmed, agency-name-sanitized response from NEOGOV's
  official per-agency GovernmentJobs RSS endpoint for `agency=fulton`.
- `freehire_search_page1.json`, `freehire_search_page2.json`,
  `freehire_detail_greenhouse.json`, and `freehire_detail_workday.json` —
  sanitized, reduced examples of Freehire's public `{data, meta}` search and
  detail contract as documented and tested by the reviewed upstream v1.6.0
  client. Employer names, requisition values, and posting prose are synthetic;
  endpoint paths and field shapes follow that public contract.

The cache used here contains only ordinary public board responses. Fixture
records contain no applicant, profile, authentication, or personal data.
