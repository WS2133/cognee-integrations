---
name: local-ui
description: Use when opening, launching, checking, or reporting on a local or remote Cognee dashboard or UI.
---

# Cognee UI

Resolve the configured dashboard before launching anything:

```bash
python3 "${PLUGIN_ROOT}/scripts/doctor.py" --json
```

- If `dashboard_url` is present, check and use that exact URL.
- If mode is `Cloud` and `dashboard_url` is absent, report that the remote UI is
  not configured. Set it with `COGNEE_UI_URL` or `ui_url` in
  `~/.cognee-plugin/config.json`; do not substitute localhost or infer a port.
- Only launch a local UI when mode is `Local` or `Local Managed`.

## Local launch

From the Cognee repository root:

```bash
uv run cognee-cli -ui
```

Expected local surfaces:

```text
Backend: http://localhost:8000
Frontend: http://localhost:3000
```

Keep the process running when requested. Inspect occupied ports before starting
another copy, and never kill a process without approval.

## Checks

```bash
curl -i "<server_url>/health"
curl -i "<dashboard_url>/"
```

If authenticated checks are needed, use the repository's documented local test
credentials only when appropriate and do not expose real user credentials.

Report the exact dashboard URL, backend health, and any broken authenticated flow.
