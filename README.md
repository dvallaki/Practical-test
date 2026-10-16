# Survey reporting slice

## 1. Running it

```
docker compose up --build
docker compose exec backend python -m app.ingest    # creates the tables, loads both CSVs; safe to re-run
docker compose exec backend pytest                  # test_summary.py skips until the ingest has run
```

- No migration tool: tables are created with `create_all()` (also runnable alone: `python -m app.models`).
- `rejects.csv` is written to [backend/rejects.csv](backend/rejects.csv), because `/data` is mounted read-only.
- If something else on your machine already holds port 5432 or 8000, `db`/`backend` silently fail to start and the page shows `GET /api/surveys -> 500`. I hit this; free the ports.

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
- `client.timezone` (IANA name, default `Asia/Kolkata`) is the zone a client's reporting week (Saturday to Friday) is cut in. "Saturday" is a local-calendar concept. Cutting Dubai's weeks in Kolkata time would put the boundary at 22:30 Dubai time. The report takes the local date (`started_at AT TIME ZONE client.timezone`) and steps back to the previous Saturday.
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
- **`parse_client_datetime`** accepts exactly `%d/%m/%Y %H:%M:%S` and attaches `Asia/Kolkata`. Impossible dates (`29/02/2023`), other formats (ISO, date-only, an explicit offset) and non-ASCII digits raise `ValueError`. No guessing, and no month/day swapping.
- **`normalise_email`**: canonical = `strip()` + lower-case the whole address, then a shape check (one `@`, no whitespace, dotted domain). Lower-casing the local part is technically not RFC-safe, but no real mail provider treats case as significant, and the data shows the same person in different cases across the three systems (`Priya.Sharma22@` vs `priya.sharma22@`). I deliberately *don't* strip dots or `+tags`. That's Gmail-specific and would merge distinct people on other domains.
- **`parse_duration_seconds`** accepts integers, thousands separators (`2,460` → 2460; the row's timestamps are 41 minutes apart, confirming it), and decimals (rounded half-up). It returns negatives as-is: `-2400` *is* a number. Rejecting it is a business rule, so the ingest does it.
- **Tests:** [backend/tests/test_normalize.py](backend/tests/test_normalize.py) runs the cases above, plus every distinct value of each column in `responses.csv` through its parser. The only failure mode is `ValueError`, and the only values that fail are the known bad rows.

## 4. What I found wrong in the brief

- **3,265 responses (9%) start before their survey's `created_date`.** E.g. survey 12 "Q2 Employee Pulse" is created 01/04/2026 but 1,345 of its 3,261 responses are from Jan–Mar, and every survey has responses from 1 January. I loaded them rather than reject 9% of the data on a rule the client never gave. My guess is that `created_date` is when the survey was created in the newest system after a migration, not its launch date. **Question for the client.**
- **"A respondent counts only once per survey" doesn't say which attempt counts.** 578 people have 2–3 different attempts at the same survey. I count the most-progressed, then latest, and keep the rest, so the rule is one `UPDATE` to change.
- **Future dates aren't covered by the rules.** I reject `started_at` / `completed_at` later than the time of the run (`31/12/2027`).
- **`partial` responses all have a `completed_at`.** That's odd for something not completed, but consistent across ~11k rows, so I load it as-is.
- **"responses_started" contradicts rule 3.** The rule says the response count is completed + partial, and that `started` and `abandoned` count in neither. So a field called `responses_started` must *exclude* responses whose status is `started`. I followed the rule, not the name: `responses_started` = completed + partial. The name should be `responses_received` or similar. **Question for the client.**
- **Weekly completion rate over *total* invitations.** Invitations aren't dated, so each week's rate is that week's completions over the whole survey's invitations. The weekly numbers are small (survey 8: 0.7%, 2.1%…) and they *add up* to the survey's overall rate instead of averaging to it. That's what the rule says, so I implemented it, but it's worth confirming it's what they expect to see per week.
- **Which date puts a response in a week isn't stated.** I use `started_at`, so a response and its completion stay in one row. The alternative (count completions in the week they completed) differs for only 23 of 16,175 counted completed responses, the ones that cross a Friday→Saturday midnight.
- **"Saturday to Friday" in which clock?** In the client's local time (`client.timezone`). Doing it in UTC would move 8,642 responses (23%) to a different weekday, so the numbers wouldn't tie out with their other reports.
- **Median duration of which responses?** Not specified. I use completed responses only ("how long does it take to do the survey"). Partials would drag it down with half-finished sessions. `null` in a week with no completions.
- **Empty weeks.** Weeks between a survey's first and last response with no responses come back as zero rows, not missing, so the table doesn't silently skip a week.
- **"Under 300 ms" doesn't say measured where, or at what percentile.** I measured server-side over HTTP and report p95 and max (section 5).
- **Survey 9 "Pilot - Do Not Report"** (client "Internal", 0 invitations, no responses) is loaded, because the export lists it. Whether it should appear in the list at all is a question for the client. I kept it in the list, since "Do Not Report" arguably still means "show that it exists". Its summary returns `weeks: []`, and completion rate over 0 invitations is `null`, not a divide-by-zero.

## 5. Performance

**Every request under 300 ms. Slowest of 600 was 166 ms; p95 72 ms.**

Measured with the full dataset loaded (37,997 responses), over real HTTP against the running uvicorn server (`--reload`, one worker), from inside the backend container. Each of the 12 surveys was requested 50 times after a restart; timings are wall-clock per request with `httpx` and `time.perf_counter()`:

| | p50 | p95 | max |
|---|---|---|---|
| all 600 requests | 35 ms | 72 ms | 166 ms |
| survey 5 (largest, 11,710 counted rows) | 70 ms | 152 ms | 166 ms |
| survey 9 (no responses: the fixed overhead) | 23 ms | 49 ms | 76 ms |
| first request after restart (survey 5) | 126 ms | | |

This laptop was also running another project's PHP/MySQL stack during the run, so these numbers are pessimistic.

Why it's fast: one SQL statement per request, aggregated in Postgres. `EXPLAIN (ANALYZE, BUFFERS)` for survey 5 shows an **Index Only Scan on `ix_response_summary`, Heap Fetches: 0**, 87 buffers, ~24 ms. That depends on the ingest running `VACUUM ANALYZE` at the end, so the visibility map is current. Without it, Postgres has to visit the table for every row.

Correctness check: [backend/tests/test_summary.py](backend/tests/test_summary.py) recomputes every survey's weekly numbers in Python from the raw counted rows, using an independent week rule (`zoneinfo` + `date.weekday()`), and compares them field by field with the endpoint. It also tests each client rule separately (Saturday starts and contiguous weeks, the rate denominator, partial vs completed), plus survey 9 and a 404 for an unknown survey.

## 6. The delete (Step 6a)

The two emails only conflict if the audit table holds the *responses themselves*. Compliance's rule is a legal requirement from the customer's legal team. It outranks a reconciliation process, and a later email from a different person doesn't override it. So:

1. **Responses are hard-deleted**, in one transaction: the survey's `response` rows, then any `respondent` rows no other survey references (the email *is* the personal data), then the survey. Not a soft-delete flag: "gone permanently" means gone.
2. **The audit keeps counts, not responses.** Before the delete, the same transaction writes a `survey_deletion_audit` row: survey id, name, client, invitations, who deleted it, when, and response counts by status and reporting week. That is exactly what year-end reconciliation needs. It holds no response ids, emails, names, ratings or free text. Note there is no audit table today; "the audit table" in the email needs defining.
3. **Re-ingest must not resurrect it.** The client's CSV export will still contain the survey, so the ingest skips survey ids in the deletion audit and logs them to rejects.csv as `survey deleted`.
4. **Before building, get both people to agree in writing** that an aggregate record satisfies the reporting manager and doesn't breach compliance. Also confirm with compliance what "permanently" means for database backups (usually: gone when the backup retention window expires) and for rejects.csv. Until they agree, the delete button doesn't ship.

## 7. AI log

**Tool:** Claude Code (Claude Opus) in the terminal, for every step. **About 95% of the final code and README text came from it.** My part was setting direction, reading every diff and output, and making the calls in section 4. Nothing was accepted on the strength of "it ran". Each step was checked against the data or the running system: idempotency via `xmin` and sequence values, the summary against an independent Python recomputation, and the page via a scripted click-through of all 12 surveys.

**Not accepted #1: the bulk load (Step 2).** The first ingest upserted through SQLAlchemy's executemany:

```python
# before: version 1
stmt = insert(Response)
upsert = stmt.on_conflict_do_update(index_elements=["id"], set_=..., where=_changed(...))
for i in range(0, len(rows), BATCH_SIZE):
    db.execute(upsert, rows[i : i + BATCH_SIZE])
```

The run was still going after 5 minutes. `pg_stat_activity` showed one INSERT at a time, so the "batch" was really row by row. I killed it, and the single transaction rolled back to 0 rows. The tool's second attempt, one multi-row `insert(Response).values(batch)` per 2,000 rows, finished, but in 70–95 s. A cProfile run showed ~46 s of that was SQLAlchemy compiling bind parameters. I didn't accept either version. The final one copies valid rows into a temp table and upserts with set-based SQL:

```python
# after
with cur.copy(f"COPY stage_response ({', '.join(STAGE_COLS)}) FROM STDIN") as copy:
    for r in rows:
        copy.write_row([r[c] for c in STAGE_COLS])
# then: INSERT INTO respondent (email) SELECT DISTINCT ... WHERE NOT EXISTS (...)
# and:  INSERT INTO response (...) SELECT ... FROM stage_response JOIN respondent ...
#       ON CONFLICT (id) DO UPDATE SET ... WHERE (...) IS DISTINCT FROM (...)
```

17–34 s end to end, most of it container start-up. And a re-run still writes nothing.

**Not accepted #2: rounding in the API (Step 5).** Before: `"completion_rate": round(row.responses_completed / invitations, 4) if invitations else None`. Rounding to 4 dp turns 1 completion out of 15,400 invitations into exactly 0.0001, so small weekly rates lose their precision before they reach the page. After: `row.responses_completed / invitations if invitations else None`, with the page doing `(rate * 100).toFixed(2) + "%"`. Display rounding belongs to the display, and the test now compares the exact value.

**Faster than alone: profiling the CSV (Step 1).** In a few minutes of scripted profiling it surfaced:
- the planted edge-case rows (`9000xx`)
- the 578 (survey, email) pairs with repeat attempts, which turned "counts once" into a schema decision (`is_counted` plus a partial unique index)
- the 3,265 responses dated before their survey was created
- that 23% of responses fall on a different weekday in UTC than in Kolkata

By hand in 38k rows, that would have eaten most of the two hours, and I'd have designed the schema before knowing any of it.

## 8. Where the two hours went

**It took about 3 hours, not 2.** I started around 12:00, and the last commit is 15:05. When I hit the two-hour mark during Step 5, I chose to finish all six steps rather than stop and list the rest. In hindsight, the brief says that's the worse answer: I should have stopped and written down what was left.

| Step | Brief's guide | Actual (from commits) |
|---|---|---|
| 1. Data model | 20 min | ~23 min |
| 2. Ingest (+ normalize.py bodies, which it needs) | 30 min | ~47 min |
| 3. normalize.py tests | 15 min | ~21 min |
| 4. Report endpoint | 20 min | ~21 min |
| 5. The page | 15 min | **~60 min** |
| 6a + 6b | 20 min | ~13 min |

**What took longer than planned:**
- **Step 5, by 45 minutes.** The component itself was quick. The time went on two things: working out why the page showed `GET /api/surveys -> 500` (another local project held ports 5432 and 8000, so this stack's db and backend never started), and getting a headless browser running for the click-through (the local Chromium was missing system libraries, so I used the Playwright Docker image).
- **Step 2, by ~17 minutes:** the slow bulk load in section 7.
- **The README length is not extra hours.** The AI drafted each section as its step finished, from output I'd just checked. That's why it's long and why it didn't cost proportionally more time. With more time I'd cut it, except section 2, which carries the most marks.

**Left out:**
- unit tests for the ingest's business rules (today they're verified only by running against the real file)
- front-end tests
- an Alembic migration (`create_all()` only)
- deleting rows that disappear from the export
- the delete itself (design only, per the brief)

**Next two hours, in priority order:**
1. Get the section 4 questions answered by the client, especially `responses_started` and `created_date`. They can change numbers on the screen.
2. Unit tests for each reject reason, the counted-attempt pick, and "a changed row is updated on re-run".
3. Handle rows that disappear from the export: today a re-run never deletes, so a response removed upstream stays in the report forever.
4. An Alembic migration in place of `create_all()`.
5. Survey delete, as designed in section 6.

**Least happy with:** "started_at is in the future" is judged against the clock at run time, so the ingest depends on when you run it: run the same file in 2028 and row `900018` loads. A fixed cutoff, such as the export's own date, would make it deterministic.
