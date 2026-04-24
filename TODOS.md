# TODOS

## Port 587 Fallback for Blocked Port 25 Environments

**What:** When port 25 startup probe fails, retry verification on port 587 with STARTTLS.

**Why:** Port 25 is blocked on AWS, GCP, Azure, and many corporate networks. Port 587
is often open. If 587 works for anonymous RCPT TO checks (not guaranteed — many servers
require AUTH on 587), this would enable the tool to work from cloud infrastructure.

**Pros:** Tool works from more hosting environments if the fallback is viable.

**Cons:** Most mail servers on 587 require AUTH, so anonymous RCPT TO verification
may not work. Risk of building a fallback that silently fails.

**Context:** v1 targets home machine or Hetzner/OVH/Linode VPS. 587 fallback is a
post-v1 investigation. Test manually against Google Workspace and Microsoft 365 on
port 587 before committing to build this.

**Depends on:** Port 25 startup check in main.py startup event.

---

## CSV Batch Upload Size Limit

**What:** Add row count validation on `POST /batch` before parsing. Reject CSVs
over `MAX_BATCH_SIZE` rows (default: 1000).

**Why:** Parsing a 500,000-row CSV into memory exhausts RAM and crashes the server
process. Without a limit, a careless upload takes down the entire tool.

**Pros:** 3-line fix. Gives a clear 400 error (`CSV too large — max 1000 rows`)
instead of silent OOM crash.

**Cons:** May frustrate users with large lists, but 1000 contacts already takes
~5-10 minutes to verify.

**Context:** `MAX_BATCH_SIZE` should be configurable via env var. Default 1000.
In `main.py`, after parsing the CSV: `if len(contacts) > MAX_BATCH_SIZE: raise HTTPException(400, detail=f"CSV too large — max {MAX_BATCH_SIZE} rows")`.
