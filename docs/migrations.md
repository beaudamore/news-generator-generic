# Database Migrations

The database schema is versioned. Every database records the migrations applied to it in the
`schema_version` table, and code ships with the list of migrations it needs in
`services/migrations.py`. A code change that touches the schema is not complete until it has a
migration entry here and in that file.

## How the database is created and updated

| Situation | What happens |
|---|---|
| First start, no database file | Tables are created from `services/models.py` and the database is stamped at the latest version. Nothing is "migrated"; a fresh database already matches the models. |
| Existing database, behind the code | Every migration above the recorded version runs in order, each in its own transaction, and is recorded on success. A failing migration rolls back and leaves the version where it was. |
| Existing database, current | Nothing happens. |

Both the app (on startup, in `services.db.init_db`) and the script do exactly this:

```text
python scripts/init_db.py            create if missing, then apply pending migrations
python scripts/init_db.py --status   report current and latest versions; change nothing
```

The `DATABASE_URL` and `DATA_DIRECTORY` values in `.env` decide which database is touched.

## Rule: every schema change needs a migration

Any change to `services/models.py` (a new table, a new or removed column, a changed type, a
constraint) and any data transform a code change depends on (moving values between columns,
backfilling, dropping a table that moved to disk) must ship as a numbered migration.

The header of `services/models.py` repeats this rule so it is seen at the point of change.

`services.db._add_missing_columns` still adds columns the models have and the database lacks.
That is a safety net for development databases, not a substitute: it cannot drop, rename,
retype, or move data, and production databases must not depend on it.

## Adding a migration

1. Change the model in `services/models.py`.
2. In `services/migrations.py`, write a function `_vN_short_name(connection)` that brings an
   existing database from version N-1 to N. It must be safe on production data: check before
   dropping, copy values before retyping (SQLite cannot alter a column type; rename the column,
   add the new one, copy, drop the old), and never assume the table is empty.
3. Append `Migration(N, "what and why", _vN_short_name)` to `MIGRATIONS`. N is the next
   consecutive integer; the module asserts this at import.
4. Add a row to the version history below.
5. Run `python scripts/init_db.py` against a copy of a real database and confirm
   `--status` reports the new version and the data survived.

Never edit or remove a migration that has shipped. Fix a bad migration with a new one.

## Updating a production database

1. Stop the app.
2. Back up the database file (SQLite: copy `news.db`, `news.db-wal`, and `news.db-shm` together).
3. Deploy the new code.
4. Run `python scripts/init_db.py --status` and read the pending list.
5. Run `python scripts/init_db.py` and confirm it reports the expected `from -> to`.
6. Start the app. It runs the same check on startup and finds nothing pending.

If step 5 fails, the failed migration rolled back and the version is unchanged. Restore from the
backup only if the failure left partial data; otherwise fix the migration and rerun.

## Version history

| Version | Date | Change | Why |
|---|---|---|---|
| 1 | 2026-09-15 | Baseline: tenants, api_keys, prompts, voices, search_profiles, schedules, runs, usage_events, audit_events, and the schema_version table itself | Versioning begins; tables as created by the models at this point |
| 2 | 2026-09-15 | Drop the `avatars` table | Avatars live only as `<name>.png` plus `<name>.json` under `assets/avatars/<tenant>/`; the table duplicated the files and drifted from them |
| 3 | 2026-09-15 | Add `summarize_prompt`, `video_visual_prompt`, `video_sound_prompt` to `search_profiles`, fill them from each tenant's `prompts` rows, drop the `prompts` table | Prompts are per profile: a new profile is filled from the default files, and every save stores the profile's own text. Defaults are files edited by the admin tenant |
| 4 | 2026-09-15 | Add `hero_prompt` and `hero_workflow` to `search_profiles`, filled from `prompts/hero-image.md` and `HERO_COMFYUI_WORKFLOW` | Segment hero images: each profile carries the instruction that turns a segment into an image prompt and the image template used to render it |
