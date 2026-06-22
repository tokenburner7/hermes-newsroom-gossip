# BUG_LIST.md — scripts/cycle.py

## HIGH

### H1. `npm` and `npx` not resolved with absolute paths (lines 325, 356)
**Impact:** Cron has minimal PATH. If `npm`/`npx` aren't in `/usr/local/bin` or `/opt/homebrew/bin`, build+deploy fail silently with `FileNotFoundError` swallowed by `OSError` handler.
**Fix:** Resolve `npm` and `npx` with `shutil.which()` like we do for `uv`, fall back to bare names.

### H2. Kill-switch produces misleading error log (line 308-311)
**Impact:** When kill-switch is ON, run-once exits rc=1 with "kill-switch is ON". parse_run_output sees no fact-gate verdict, logs "ERROR: no fact-gate verdict". Operator sees CRITICAL in logs for a normal state.
**Fix:** Check for "kill-switch is ON" in output before the fallthrough error, mark status as "kill_switch".

## MEDIUM

### M1. `db_url()` modifies os.environ via load_dotenv (line 157)
**Impact:** `load_dotenv()` by default OVERRIDES existing env vars. If cron pre-sets DATABASE_URL, .env file could silently replace it.
**Fix:** Pass `override=False` to load_dotenv.

### M2. `psycopg` re-imported on every DB call (lines 174, 199)
**Impact:** Minor — import is cached by Python after first call. But top-level import with lazy error handling is cleaner.
**Fix:** Move to top-level import with try/except, use a module-level `_psycopg = None` sentinel.

### M3. `_EXTRA_PATH` missing `/usr/bin` (line 71)
**Impact:** On macOS, system Node/npm installed via Xcode or pkg lives in `/usr/bin`. If neither Homebrew path has npm, build fails.
**Fix:** Add `/usr/bin` to _EXTRA_PATH.

## LOW

### L1. `.json.tmp` orphaned on cross-device `replace()` (line 143-145)
**Impact:** If `/Users/tn/.hermes/` is on a different filesystem than /tmp, `Path.replace()` raises OSError. The `.json.tmp` file is left behind but gets overwritten next cycle.
**Fix:** Accept — `shutil.move` would have the same issue. Not worth the complexity.

### L2. Env parser silently skips `export KEY=value` lines (line 106)
**Impact:** `partition("=")` gives key = `export KEY` which won't match any expected var name, silently dropped.
**Fix:** Strip `export ` prefix before parsing. But our .env files don't use `export`. Defer.
