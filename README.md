# Survey reporting slice

## 1. Running it

```
docker compose up --build
docker compose exec backend python -m app.models    # creates the tables (idempotent, create_all)
docker compose exec backend python -m app.ingest    # also creates the tables; safe to re-run
```

`rejects.csv` is written to [backend/rejects.csv](backend/rejects.csv) (`/data` is mounted read-only; `backend/` is the writable mount).

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

| | |
|---|---|
| `surveys.csv` rows loaded | 12 of 12 (4 clients) |
| `responses.csv` data rows | 38,005 |
| Responses loaded | **37,997** |
| …of which counted (one per person per survey) | 37,414; the other 583 are earlier/lesser repeat attempts, kept with `is_counted = false` |
| Distinct respondents | 35,162 |
| Rows rejected | **8** (all from `responses.csv`) |

Distinct reasons (a row can have more than one; `reason` in rejects.csv joins them with `; `):

| Reason | Rows | Example |
|---|---|---|
| started_at / completed_at is not a real timestamp | 1 | `900006`: `29/02/2023` (2023 is not a leap year) |
| started_at / completed_at is in the future | 1 | `900018`: `31/12/2027` |
| completed_at is before started_at | 2 | `900007` (day/month look swapped), `900008` |
| duration_seconds is negative | 1 | `900008`: `-2400` |
| rating out of range 1-5 | 1 | `900009`: `9`, rejected, not clamped |
| invalid respondent_email | 1 | `900019`: `not-an-email`. With no identity we can't apply "counts once", so the row is rejected. |
| unknown survey_id | 1 | `900010`: survey `47` isn't in surveys.csv |
| duplicate response_id (identical row) | 1 | `900011` appears twice, byte-identical. The second copy is logged, the first is loaded. |

**Running it twice changes nothing.** Verified by comparing row counts, `max(xmin)` (Postgres' row-version id), and the respondent sequence before and after a second run: all identical, and `rejects.csv` is byte-identical. Upserts are keyed on the natural keys and carry `WHERE (cols) IS DISTINCT FROM (EXCLUDED.cols)`, so an unchanged row isn't rewritten. New respondents/clients are inserted only if absent (no sequence burn).

How it handles the rules:

- **Bad rows don't stop the import.** Every row is validated in Python first, collecting *all* problems rather than the first. Only clean rows reach the database. The whole load is one transaction, so a crash (killed mid-run while testing) leaves the previous state, not half a load.
- **Ratings** outside 1–5 are rejected. The DB CHECK backs this up. Empty markers (`''`, `N/A`, `NULL`, `-`) are "no rating", not errors. 10,880 rows, including completed ones, load with `rating = NULL`.
- **Timestamps:** parsed strictly as `DD/MM/YYYY HH:MM:SS` in `Asia/Kolkata` and stored as `timestamptz` (UTC). Dubai rows will go through the same column. See section 2.
- **Once per survey:** respondent identity is `normalise_email()`. Repeat attempts are kept. The counted one is the furthest status (completed > partial > started = abandoned), ties broken by latest `started_at`, then highest id. This is a rule I chose. See section 4.
- **Status** is case-folded (`COMPLETED`/`Completed`/`completed` → `completed`). **Channel** blank → NULL. **free_text** placeholder markers → NULL. Real text (quotes, newlines, Devanagari, emoji in names) is stored untouched.
- **Speed:** valid rows are `COPY`'d into a temp table and upserted with two set-based `INSERT ... SELECT` statements. A first version using SQLAlchemy bulk `INSERT ... VALUES` took ~100s, mostly compiling bind parameters.

### normalize.py decisions

- **Empty markers**, shared by all three functions and found by reading the file: `''` / whitespace, `N/A`, `n/a`, `NULL`, `null`, `-`, `none`, compared case-insensitively.
- **`parse_client_datetime`** accepts exactly `%d/%m/%Y %H:%M:%S` and attaches `Asia/Kolkata`. Impossible dates (`29/02/2023`) raise `ValueError`. No guessing other formats, and no month/day swapping.
- **`normalise_email`**: canonical = `strip()` + lower-case the whole address, then a shape check (one `@`, no whitespace, dotted domain). Lower-casing the local part is technically not RFC-safe, but no real mail provider treats case as significant, and the data shows the same person in different cases across the three systems (`Priya.Sharma22@` vs `priya.sharma22@`). I deliberately *don't* strip dots or `+tags`. That's Gmail-specific and would merge distinct people on other domains.
- **`parse_duration_seconds`** accepts integers, thousands separators (`2,460` → 2460; the row's timestamps are 41 minutes apart, confirming it), and decimals (rounded half-up). It returns negatives as-is: `-2400` *is* a number. Rejecting it is a business rule, so the ingest does it.

## 4. What I found wrong in the brief

- **3,265 responses (9%) start before their survey's `created_date`.** E.g. survey 12 "Q2 Employee Pulse" is created 01/04/2026 but 1,345 of its 3,261 responses are from Jan–Mar, and every survey has responses from 1 January. I loaded them rather than reject 9% of the data on a rule the client never gave. My guess is that `created_date` is when the survey was created in the newest system after a migration, not its launch date. **Question for the client.**
- **"A respondent counts only once per survey" doesn't say which attempt counts.** 578 people have 2–3 different attempts at the same survey. I count the most-progressed, then latest, and keep the rest, so the rule is one `UPDATE` to change.
- **Future dates aren't covered by the rules.** I reject `started_at` / `completed_at` later than the time of the run (`31/12/2027`).
- **`partial` responses all have a `completed_at`.** That's odd for something not completed, but consistent across ~11k rows, so I load it as-is.
- **Survey 9 "Pilot - Do Not Report"** (client "Internal", 0 invitations, no responses) is loaded, because the export lists it. Whether it should appear in the list at all is a question for Step 4/5; completion rate over 0 invitations must be `null`, not a divide-by-zero.

## 5. Performance

TODO

## 6. The delete (Step 6a)

TODO

## 7. AI log

TODO

## 8. Where the two hours went

TODO
