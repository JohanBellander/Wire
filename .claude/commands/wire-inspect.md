---
description: Run wire.scripts.inspect against production and summarize the output. Usage: /wire-inspect [hours]
argument-hint: "[hours] (default 24)"
---

Run the Wire production inspection script and summarize what it shows.

The user wants to see what Wire has been doing in the last `$ARGUMENTS` hours
(default 24 if no argument given). Run:

```bash
ssh johan@gary 'docker exec $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) python -m wire.scripts.inspect ${1:-24}'
```

If the script doesn't exist on the running container yet (older deploy that
predates `wire.scripts.inspect`), fall back to the standalone diagnostic in
`scripts/inspect_payload.py` piped via SSH:

```bash
cat scripts/inspect_payload.py | ssh johan@gary 'docker exec -i $(docker ps --filter name=j13i32n8rrvzsxpydl404f6v -q) python /dev/stdin'
```

After the output lands, summarize for the user:

1. **What's working** — events ingested per repo, sessions formed, healthy
   triage distribution.
2. **Top scored events** — quote 2-3 of the highest, with their reasons,
   so the user can sanity-check Wire's judgment.
3. **Drafts and decisions** — count by status, any rejections with reasons.
4. **LLM cost** — total spend for the window, fallback rate, anything
   unusually high.
5. **Anything anomalous** — empty PR titles, unscored events (triage
   failures), high fallback rate, sessions stuck open, or other surprises.

If something looks broken, propose a fix and offer to push it after user
confirmation. Don't push automatically.
