# IDC Automation

Shared infrastructure automation projects. Each project contains only reusable
code and non-secret configuration; operational inputs stay on the user's local
machine or in approved internal systems.

## Projects

- [ICE2 NetBox + live device dashboard](ice2-netbox-dashboard/README.md) — read-only
  dashboard that compares NetBox cabling with live switch state for the ICE2
  backend fabric, with one-click sync. See its README for requirements, setup,
  daily use and troubleshooting.
- [LON14 NetBox + live device dashboard](lon14-netbox-dashboard/README.md) — the same
  dashboard for the LON14 (`sys1-lon14`) InfiniBand backend fabric, with LON14's leaf
  numbering, UFM pair and jump host. Runs side by side with ICE2 on port 8766.
- [Backend fabric dashboard](multi-region-backend-dashboard/README.md) — **one service for every
  region** on http://127.0.0.1:8765/: ICE2 and LON14 engines served under /ice2/ and /lon14/,
  OSL1/STA1/GLO1 drawn NetBox-first with UFM's live links compared directly, and live
  metrics, logs and incidents through `nsr`. Start with
  `multi-region-backend-dashboard/scripts/open_regions.command`.

Each project documents its own per-user access setup. Never commit credentials,
tokens, passwords, or collected operational evidence.
