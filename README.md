# IDC Automation

Shared infrastructure automation projects. Each project contains only reusable
code and non-secret configuration; operational inputs stay on the user's local
machine or in approved internal systems.

## Projects

- [NetBox dashboard launcher](ice2-netbox-dashboard/README.md) — read-only
  NetBox/device dashboard code with per-user local-input setup.

Each project documents its own per-user access setup. Never commit credentials,
tokens, passwords, or collected operational evidence.
