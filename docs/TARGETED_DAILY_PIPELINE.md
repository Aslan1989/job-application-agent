> **Superseded by `SKILL_BASED_DISCOVERY.md` (v0.8).** This describes the v0.6/v0.7
> pipeline with seven fixed search terms and a single source; it is kept for history.

# Targeted Daily Job Pipeline

Inputs: Google Sheets `Profile` preferences (roles, locations, min score), verified local profile, an unofficial BA Jobsuche adapter and a configured LLM provider.

Flow:
1. Search up to 9 rotated term/city combinations with a bounded result size.
2. Parse both known 2026 BA and legacy schemas; filter by exact city and non-senior/non-Werkstudent tech role.
3. Deduplicate by `ba:<refnr>` against the existing 19-column Google Sheets `Vacancies` table. Never override human-made status/approval decisions.
4. Append `Found`, `Not reviewed` (only with explicit `--write`).
5. Optionally fetch job descriptions, strip HTML scripts/styles and skip incomplete descriptions.
6. Run v0.5 LangGraph for each eligible job if `--ai`; score from candidate evidence and complete vacancy text, with a bounded LLM request count.
7. `--write` updates scores only for unscored `Found` rows with `Not reviewed`, status `Review` at threshold or `Skip` otherwise. Unknown questions require human verification.
8. Always end without sending an application.

Data confidentiality: local candidate profile holds no contact information and sends minimized professional evidence to Gemini. Private repositories are not used in Gemini prompts. Keep `.env`, OAuth files and profile outside public GitHub.

Error handling: search failures logged; all-search failure aborts. Detail failures leave jobs `Found`. First LLM failure stops the remaining LLM calls; job can be retried in later runs.

Data quality: Model summaries are advisory, not hard facts. Never assume country-specific work authorization or a company security clearance requirement unless explicit in the advert.
