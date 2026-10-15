# Survey reporting slice

## 1. Running it

```
docker compose up --build
docker compose exec backend python -m app.models    # creates the tables (idempotent, create_all)
```

## 2. Data model

Four tables, defined in [backend/app/models.py](backend/app/models.py).

```
client 1──* survey 1──* response *──1 respondent
```

| Table | One row per | Primary key |
|---|---|---|
| `client` | enterprise customer (Northwind, Meridian, Halcyon, Internal) | surrogate `id` |
| `survey` | survey in `surveys.csv` | **natural** `id` = the export's `survey_id` |
| `respondent` | person, identified by canonical email | surrogate `id`, `email` UNIQUE |
| `response` | attempt in `responses.csv`, **every attempt kept** | **natural** `id` = the export's `response_id` |

### Keys

- **Natural keys on `survey` and `response`.** The export's ids are stable and unique (one exact duplicate row, `900011`, appears twice; the PK collapses it). Using them as PKs makes re-running the ingest idempotent with `INSERT ... ON CONFLICT (id)`, and lets every row in `rejects.csv` be traced back by the client's own id. If a second source system ever arrives with an overlapping id space, the fix is a `(source_system, source_id)` unique key plus a surrogate PK. I didn't build that yet because nothing needs it today.
- **`client` is its own table**, not a text column on `survey`. Client names repeat across surveys, and the client is where the timezone lives (below).
- **`respondent` is its own table, keyed by canonical email.** "A respondent counts only once per survey" needs a stable identity for a person. Emails arrive from three systems in different cases and with stray whitespace (`Priya.Sharma22@example.com` vs `priya.sharma22@example.com `). `respondent.email` holds the output of `normalise_email()`, so two rows for the same person resolve to the same `respondent.id`.
- **FKs are `ON DELETE RESTRICT`.** A delete that silently cascades through responses must be a deliberate decision (see section 6). Responses that point at a survey that doesn't exist (`survey_id = 47`) cannot be inserted. The ingest rejects them.

### "Counts once per survey": `is_counted` + partial unique index

The data has 578 (survey, email) pairs with 2–3 *different* attempts each (e.g. abandoned in February, completed in May). They're real repeat attempts, not export duplicates, so I don't throw them away. Every attempt is kept, and:

- `response.is_counted boolean NOT NULL DEFAULT false`
- `UNIQUE (survey_id, respondent_id) WHERE is_counted`: the database makes it impossible for one person to be counted twice in one survey, whatever the ingest or a later bug does.

The ingest picks the counted attempt per (survey, respondent): furthest status first (completed > partial > started/abandoned), then latest. The report reads only `is_counted` rows. Keeping the losers means the rule can change ("count the first attempt instead") with an `UPDATE`, not a re-import.

### Constraints

| Constraint | Why |
|---|---|
| `status IN ('completed','partial','started','abandoned')` | Export has 7 spellings (`COMPLETED`, `Partial`…). Stored lower-cased; anything else is rejected. CHECK on text, not a PG ENUM, so adding a status is a constraint swap, not `ALTER TYPE`. |
| `rating BETWEEN 1 AND 5` | Client rule. The row with rating `9` is rejected, never clamped. The DB enforces it even if the ingest has a bug. |
| `duration_seconds >= 0` | `-2400` exists in the data. That's a data error. |
| `channel IS NULL OR channel IN ('email','sms','whatsapp','kiosk')` | Closed set in the data. |
| `completed_at IS NULL OR completed_at >= started_at` | Two rows complete before they start (`900007`, `900008`). |
| `status <> 'completed' OR completed_at IS NOT NULL` | A completed response has a completion time. Holds for every valid row. |
| `status NOT IN ('started','abandoned') OR completed_at IS NULL` | An unfinished response has no completion time. Holds for every row. |
| `survey.invitations_sent >= 0` | It's the denominator of completion rate. Zero is allowed (the Internal pilot has 0); the report returns `null` rather than divide by it. |
| `UNIQUE (client_id, name)` on survey | Two surveys with the same name for one client would be indistinguishable in the UI. |
| `respondent.email = lower(btrim(email))`, contains `@` | Backstop so nothing bypasses `normalise_email()`. |
| name columns not blank | `client.name`, `survey.name`. |

Things I deliberately left **out of the schema** and kept in the ingest: "start date is not in the future" (`31/12/2027` exists). A CHECK against `now()` is not immutable and would make a valid row invalid as time passes, and the opposite for restores.

### Timestamps and timezones

- `started_at`, `completed_at` are **`timestamptz`**: absolute instants, stored as UTC. The ingest interprets the export's naive `DD/MM/YYYY HH:MM:SS` as `Asia/Kolkata` and converts. When the Dubai customer arrives, their rows are interpreted as `Asia/Dubai` and stored the same way. No column changes and no re-migration.
- `client.timezone` (IANA name, default `Asia/Kolkata`) is the zone a client's reporting week (Saturday to Friday) is cut in. "Saturday" is a local-calendar concept. Cutting Dubai's weeks in Kolkata time would put the boundary at 22:30 Dubai time. The report does `date_trunc` on `started_at AT TIME ZONE client.timezone`.
- `survey.created_date` is a `date`: it's a calendar date in the export, not an instant.

### Indexes

| Index | Serves |
|---|---|
| `response_pkey (id)` | idempotent upsert on re-ingest |
| `ix_response_summary (survey_id, started_at) INCLUDE (status, duration_seconds) WHERE is_counted` | The `/summary` endpoint: filter one survey, group by week of `started_at`, aggregate status/duration. Covering and partial, so it's an index-only scan over just the counted rows. |
| `uq_response_counted_per_respondent (survey_id, respondent_id) WHERE is_counted` | The once-per-survey rule (above) |
| `ix_response_survey_id`, `ix_response_respondent_id` | FK lookups over *all* attempts (survey delete/audit, a person's history). The two partial indexes above don't cover non-counted rows. Postgres doesn't index FKs automatically. |
| `respondent.email` UNIQUE | resolve email → respondent during ingest |
| `ix_survey_client_id`, `client.name` UNIQUE | FK and lookup by name during ingest |

### Deliberately nullable

| Column | Why it may be NULL |
|---|---|
| `response.completed_at` | Started/abandoned responses never finish (~10.7k rows). |
| `response.rating` | ~11k rows have no rating (`''`, `N/A`, `NULL`, `-`), including completed ones. "No rating" is not a rating, and not a reason to drop the response from response counts. |
| `response.duration_seconds` | Optional in principle. A missing duration shouldn't drop a response from the counts; the median just ignores it. |
| `response.channel` | ~7.7k rows blank. Unknown channel is still a valid response. |
| `response.free_text` | Almost always empty. Placeholder markers (`NULL`, `n/a`, `none`) are stored as NULL, not as text. |
| `response.respondent_name` | Not needed for identity (email is). One row has a blank name. Kept per response, not on `respondent`, because the same person's name is spelled differently across systems (`priya sharma` / `Priya Sharma`) and I'd rather not pick one silently. |

Everything else is `NOT NULL`. That includes `started_at`, which is present on every row and is what the reporting week is cut on, and `respondent_id`: a response with no usable email can't be deduplicated, so it's rejected (`not-an-email`, `900019`).

## 3. Ingest results

TODO

## 4. What I found wrong in the brief

TODO

## 5. Performance

TODO

## 6. The delete (Step 6a)

TODO

## 7. AI log

TODO

## 8. Where the two hours went

TODO
