# Knowledge base

What FlowPilot searches when it investigates a ticket. In a real deployment the customer's own
runbooks and their ticket history go here (exported from Jira, Freshdesk, Zendesk...).

- `runbooks/`: how to diagnose and fix known problems with GarageFlow.
- `tickets/`: past support tickets that were resolved, with their root cause.

Every file starts with a small header between `---` lines:

```
---
id: rb-maintenance-jobs          # unique, never reused (search results cite it)
title: Maintenance jobs failing or not running
type: runbook                    # runbook | ticket
tags: [no-show, waitlist, background-job]
---
```

Edit or add files, then rebuild the index (it also happens at start-up). Unchanged files are skipped.
