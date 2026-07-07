# PhotonScript - session bootstrap

Before doing anything else, read:
1. docs/HANDBOOK.md - what/where this system is (both machines, deploy loop,
   nightly flow, PixInsight pipeline, hard-won PJSR lessons, session context)
2. docs/HARDWARE.md - rig facts + open unknowns

Mission: maximize every hour of shutter time; turn it into award-winning
astrophotography.

Non-negotiables for Claude sessions (details in HANDBOOK section 6):
- Never push to GitHub; Jeremy deploys with .\deploy\deploy.ps1 "msg"
- Deliver code with cp -rf into the mounted Claude folder (never rm -rf)
- Never write into the ninashare mount (receive-only Syncthing)
- The dashboard (100.94.189.77:8100) is reachable ONLY via the
  Claude-in-Chrome extension, not from the sandbox
- No credentials handling; no AstroBin scraping for data tables
- Run pytest with explicit exit-status checks (no `| tail` masking)
