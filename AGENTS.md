# Agent contract

You are setting up or maintaining a user's media stack from this repo. This repo is a **skeleton + reference**, not a deployment. The user's deployment lives in a separate directory of their choosing.

## What's authoritative vs example

- `answers.schema.json` — **authoritative.** The supported services and answer fields.
- `stack/compose.yaml`, `stack/.env.example`, `stack/Caddyfile.example` — **examples.** Read for structure; emit fresh versions tailored to the user's answers. Stack names, container names, paths, and IPs in these files are placeholders.
- `docs/CONVENTIONS.md`, `docs/TROUBLESHOOTING.md` — **reference.** Apply when relevant; don't blindly copy paths.
- `library/encode.py`, `library/library.py` — **runnable.** Ship verbatim.

## Assembly flow

1. **Deployment dir.** Ask the user where the stack lives (e.g. `~/Services/mystack`). Create if missing.
2. **Questionnaire.** Read `answers.schema.json`; ask each field, including `stack.name`. Write `<deployment>/.mediabooboo/answers.yaml`. Validate against the schema before proceeding.
3. **Generate.** From the `stack/` examples produce the user's versions in `<deployment>/`:
   - Drop services not in `enable:`.
   - Set the Compose project name, container-name prefix, hostnames, and Tailscale hostname from `stack.name`; don't hard-code `mediabooboo`.
   - Substitute `${STACK_NAME}`, `${HOST_LAN_IP}`, `${PUID}`, `${PGID}`, `${LIBRARY_ROOT}`, etc. from answers.
   - Caddyfile: emit reverse-proxy blocks only for enabled services.
4. **Source metadata.** Write `<deployment>/.mediabooboo/source.yaml` with generator provenance:
   - `template.path` — absolute path to this repo checkout.
   - `template.remote` — Git remote URL when available.
   - `template.ref` — current branch/tag when available.
   - `template.commit` — current HEAD SHA when available.
   - `generated_at` — UTC ISO-8601 timestamp.
5. **Onboarding.** Walk the user through one-shot steps for what they enabled:
   - **plex** — claim token at https://plex.tv/claim, set `PLEX_CLAIM=`, then `docker compose up -d --force-recreate plex`. Tokens expire fast.
   - **pihole** (when `reverse_proxy.mode == internal-tld`) — set `PIHOLE_PASSWORD=`, start Pi-hole, then configure the router/DHCP server to hand out `${HOST_LAN_IP}` as DNS. The generated Pi-hole config maps `*.<tld>` to `${HOST_LAN_IP}` through `FTLCONF_misc_dnsmasq_lines`.
   - **caddy** (when `reverse_proxy.mode == real-domain`) — confirm DNS points at the host before `docker compose up -d caddy`.
   - **beszel** — bring up hub, create admin, copy SSH pubkey from the System dialog, paste into `BESZEL_KEY=`, restart `beszel-agent`. Hub adds the agent at `beszel-agent:45876`.
   - **arr suite** — link Sonarr/Radarr to Prowlarr; configure Transmission as download client (host `transmission`, port `9091`, categories `sonarr` / `radarr`); set FlareSolverr indexer proxy at `http://flaresolverr:8191`.
   - **jellyseerr** — connect Jellyfin (`http://jellyfin:8096`), Radarr (`http://radarr:7878`), Sonarr (`http://sonarr:8989`).
   - **macOS host (optional)** — keep the machine awake: `sudo pmset -a sleep 0 disksleep 0 powernap 0 womp 1 disablesleep 1`.
6. **Interactive checklist UX.** Treat onboarding as an active task list, not a one-time info dump:
   - Build the checklist only from services in `enable:` and the selected `reverse_proxy.mode`.
   - Separate steps the agent can run from steps the user must do in a web UI, router UI, Plex claim page, or host settings.
   - For every manual step, give the exact URL/UI path, values to paste, and the success signal to look for.
   - Keep checklist state in the conversation and update it as the user completes steps. Don't write planning or status files into the deployment dir.
   - When blocked on user action, ask the user to perform the next step and say when it is done. Do not assume app-level integrations are configured just because containers are reachable.
7. **Verification after onboarding.** When the user says they completed the steps, test the stack before calling setup done:
   - Run `docker compose config --quiet`, inspect `docker compose ps`, and check health/logs for enabled services.
   - Probe from inside the Compose network with a disposable container when possible; verify service DNS names, direct HTTP endpoints, Caddy host routes, and Pi-hole internal-TLD DNS when enabled.
   - Verify app-level wiring through APIs or config where possible: Prowlarr has Sonarr/Radarr applications, Sonarr/Radarr have Transmission download clients with the right categories, Prowlarr has the FlareSolverr proxy when enabled, Jellyseerr can reach Jellyfin/Radarr/Sonarr, and Beszel can reach `beszel-agent:45876` after `BESZEL_KEY` is set.
   - Read API keys from generated service config files when available. If a check requires credentials or a UI-only confirmation, tell the user exactly what remains to verify and keep that checklist item open.
   - If testing alongside another running stack, avoid publishing conflicting host ports by using a temporary Compose override or disposable test network. Do not change the generated deployment just to make the test easier.
8. **Library tooling** (if `library` is in `enable:`) — drop `library/encode.py` and `library/library.py` onto the user's PATH (symlink, alias, or copy). Point them at `paths.library`. Encoding policy and naming rules live in `docs/CONVENTIONS.md`.

## Update flow

1. Read `<deployment>/.mediabooboo/source.yaml` to find `template.path`. If it is missing, ask the user for the template repo path.
2. `git -C <template.path> pull` — fetch latest skeleton.
3. Validate the existing `<deployment>/.mediabooboo/answers.yaml` against the current schema. If new required fields are missing, ask only for those fields, update `answers.yaml`, then continue.
4. Re-run assembly into a scratch dir using the existing `<deployment>/.mediabooboo/answers.yaml`.
5. Diff scratch vs `<deployment>/`. For each change: show the user, ask apply / skip / merge.
6. On apply, update `<deployment>/.mediabooboo/source.yaml` to the new template ref/commit and timestamp.

If `<deployment>/.mediabooboo/answers.yaml` lists `manual_overrides:`, never overwrite those files — only show the would-be diff.

## What you don't do

- Don't ship `stack/compose.yaml` to the user as-is. Generate fresh from their answers.
- Don't preemptively scaffold `manual_overrides`. Add it only when the user hand-edits.
- Don't add supported services or persistent answer fields without first updating `answers.schema.json`. Runtime secret placeholders may appear in `.env` when onboarding explains how to fill them.
- Don't store the template repo path in `answers.yaml`; keep generator provenance in `source.yaml`.
- Don't write planning or status docs to the deployment dir. The user's deployment holds running config + `.mediabooboo/{answers.yaml,source.yaml}` — nothing else.
