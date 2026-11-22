# AI Matching Architecture — v0.5

```mermaid
flowchart LR
  A[Local job JSON incl. full description] --> B[Validate job]
  C[Curated CV JSON] --> D[Minimize: no identity/private repos]
  B --> E[LangGraph: LLM Match]
  D --> E
  E --> F[Validate strict result]
  F --> G{Score >= Sheet threshold AND not ineligible?}
  G -->|yes| H[Review]
  G -->|no| I[Skip]
  H --> J{--write explicit?}
  I --> J
  J -->|no| K[Print dry-run]
  J -->|yes| L[Save score + status only if unscored and human-untouched]
```

Sources of truth: `data/candidate_profile.json` holds confirmed professional history and candidate evidence; Google Sheets `Profile` provides operational threshold on writes; `Vacancies` is the tracker.

The model's raw JSON must contain `score`, `summary`, `matched_skills`, `missing_skills`, `critical_gaps`, `language`, `track` and `eligibility`. Every field is checked for presence and type before saving. No action tools are exposed to the model; it only returns a structured assessment.

Single-writer requirement: Google Sheets does not offer compare-and-swap updates via this simple values API. Avoid concurrent writes, and never treat the scoring step as user approval.
