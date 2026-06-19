# Workday Adapter

This package is the local, deterministic execution layer for Workday form stages.
It keeps Workday control handling out of `mcp_servers/playwright_server.py` so the
feedback loop can run against sanitized fixtures instead of a full live Boeing
application.

## Files

- `handlers.py`: field and repeatable-section handlers with max two field attempts.
- `browser.py`: replay browser launcher that prefers `WORKDAY_REPLAY_BROWSER` or system Chrome.
- `replay.py`: local stage replay runner; no login and no live Workday URL.
- `fixtures/my_information.html`: sanitized country selector fixture.
- `fixtures/education.html`: sanitized repeatable education fixture with prompt search and rerender.
- `fixtures/experience.html`: sanitized repeatable experience fixture.
- `fixtures/stage_data.json`: redacted data copied from existing Boeing run artifacts.

## Replay

```powershell
python .\adapters\workday\replay.py --stage education
```
