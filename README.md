# IDC Automation

Shared infrastructure automation projects. Each project contains only reusable
code and non-secret configuration; operational inputs stay on the user's local
machine or in approved internal systems.

## Projects

- [ICE2 NetBox + live device dashboard](ice2-netbox-dashboard/README.md) — read-only
  dashboard that compares NetBox cabling with live switch state for the ICE2
  backend fabric, with one-click sync. See its README for requirements, setup,
  daily use and troubleshooting.

Each project documents its own per-user access setup. Never commit credentials,
tokens, passwords, or collected operational evidence.
