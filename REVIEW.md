**Block.** Findings 1–4 are blockers: 1, 2 and 4 let anyone read, change or destroy data, and 3 reports a failed delete as a success. Finding 5 needs fixing before this ships, but isn't a security issue.

The tests passing isn't evidence here. They only check status codes, and they pass *because of* finding 1: the token in `review_this_test.py:7` has no signature, and the API accepts it.

## Top five, worst first

**1. Anyone can log in as anyone: `review_this.py:29`**
`jwt.decode(token, options={"verify_signature": False})` never checks the signature, so anyone can write `{"sub": "<any user id>"}` and become that user, admins included, and none of the three endpoints checks that the user belongs to the survey's organisation either. Verify with `jwt.decode(token, SECRET, algorithms=["HS256"])`, and add that ownership check to every endpoint. Read the secret from the environment, and rotate the one hard-coded on line 24, because it's in git history now.

**2. SQL injection in the export: `review_this.py:46-50`**
`sort_by` and `order` are free-text query parameters pasted straight into the SQL, so a caller controls part of the query: `?sort_by=(CASE WHEN (SELECT ...) THEN id ELSE rating END)` reads any table in the database one answer at a time, including `users`. Combined with finding 1, that's anyone on the internet. Map `sort_by` and `order` through an allow-list (`{"created_at", "rating"}`, `{"asc", "desc"}`), and pass values as bound parameters (`:survey_id`), never via f-strings.

**3. Delete says it worked when it didn't: `review_this.py:94-100`, `ReviewThis.tsx:31-34`**
`except Exception: pass` swallows every failure without a rollback and returns `{"deleted": id}` with a 200, even for a survey that doesn't exist (which `test_delete_survey` asserts), and `handleDelete` fires the request unchecked and removes the row with no confirmation. So one mis-click permanently deletes a survey and all its responses, and a failed delete still looks like a success. Let errors propagate, return 404 for an unknown id, and in the UI confirm first and remove the row only after a 2xx; get product sign-off before merging too, because compliance and audit disagree about deleting responses.

**4. Stored XSS: `ReviewThis.tsx:50-53`**
`dangerouslySetInnerHTML` renders the user-entered survey description as raw HTML, so a description containing `<img src=x onerror=...>` runs script in every viewer's browser, with their session. Render it as text: `{surveys[0]?.description}`. If we really need rich text, sanitise it with DOMPurify and agree on that as a team.

**5. Export crashes on real data and silently truncates: `review_this.py:61`, `:41`/`:49`**
`float(r.rating)` raises `TypeError` for any response without a rating, which is common in our data (partials, N/A), so the export 500s for most real surveys; the test only passes if survey 1 happens to have none. `limit=1000` also caps "export every response" without telling the caller, so a 12k-response survey quietly exports 1,000 rows. Return `None` for a missing rating, and either stream the full export or paginate with an explicit `next` cursor.

## One thing that's fine

`survey_id` and `limit` are also interpolated into that f-string on lines 48–49, which looks like the same injection, but it isn't. FastAPI has already validated both as `int` (anything else is a 422 before your code runs), so they can only ever be numbers. The danger is specifically the two `str` parameters. When you fix finding 2, use bound parameters for all of them anyway, so the next person doesn't have to work out which ones are safe.

---

Happy to pair on the auth fix. The rest of this PR is a reasonable shape, and these are the things worth learning to spot in generated code: it reaches for whatever makes the request succeed (`verify_signature: False`, `except: pass`), and tests that only check status codes can't tell you when it has.
