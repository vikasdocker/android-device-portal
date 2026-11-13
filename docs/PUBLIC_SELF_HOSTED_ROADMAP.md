# Public self-hosted deployment roadmap

**Target:** `https://androidportal.blueflute.in`  
**Owner:** BlueFluteX  
**Hosting:** User-owned Windows PC/server  
**Infrastructure budget:** ₹0/month target (existing hardware, domain, internet and Cloudflare account assumed)  
**Initial capacity:** Up to three concurrent Android sessions, subject to host qualification  
**Access model:** Public registration with mandatory authenticated accounts and strict quotas

> This is a staged engineering plan. The current application is a single-operator portal and must not be exposed publicly until the security and isolation milestones below are implemented.

## Product definition

A developer can register, sign in, reserve an available Android emulator, upload an APK, install and launch it, interact with the screen in a browser, inspect Logcat, take screenshots, and end the session. A user can only see and control their own session and artifacts.

The existing 16-profile sequential QA workflow remains available to the owner/admin. The public service starts with three independent emulator slots, not three full 16-device matrices.

## Host qualification gate

Current owner-host estimate (13 Nov 2026): Intel Core i5-13450HX (10 cores / 16 logical processors), 16 GB RAM, NVMe storage, GPU and upload bandwidth not yet identified. CPU and storage are promising; 16 GB RAM makes three concurrent AVDs a constrained experiment, not an approved production capacity. Begin with one and two sessions; only approve three if measured free RAM, CPU, boot stability and stream quality pass the acceptance gate. A 32 GB RAM upgrade is the recommended direction if three concurrent sessions are a firm requirement.

Phase 0 scripts: `scripts/windows/diagnose-host.ps1` performs read-only inventory and acceleration checks; `scripts/windows/measure-avd-load.ps1` samples resource usage while one, two or three AVDs are manually running.

Before enabling public sessions, run and record:

- Windows version and x64 architecture.
- CPU model, physical/logical cores and virtualization support.
- Installed RAM and available RAM under idle load.
- Free SSD space and sustained disk performance.
- GPU model and emulator graphics compatibility.
- Android SDK, emulator and platform-tools versions.
- `emulator -accel-check` output; require usable WHPX on Windows.
- Actual cold-boot and steady-state RAM/CPU for one, two and three AVDs using the Phase 0 sampler; include realistic browser-streaming load.
- Internet upload bandwidth, latency, data cap and power/UPS reliability.

Do not assume three concurrent AVDs are safe merely because three profiles exist. Start with one slot, measure, then two, then three. A practical minimum target is 16 GB RAM for experimentation; 32 GB or more is preferable for three concurrent sessions, depending on host load and AVD configuration. The emulator requires CPU virtualization support and hardware acceleration for acceptable performance.

## Target architecture

```text
Public browser
  | HTTPS
Cloudflare DNS / Tunnel / WAF / rate limits
  |
Windows host: reverse proxy + FastAPI control plane
  |-- Authentication and account lifecycle
  |-- PostgreSQL (or SQLite only for single-node early beta with migrations)
  |-- Session allocator and quota enforcement
  |-- Private APK/artifact storage
  |-- Admin and audit events
  |
Windows session supervisor (privileged, private API)
  |-- Slot 1: isolated AVD + per-session ADB endpoint
  |-- Slot 2: isolated AVD + per-session ADB endpoint
  |-- Slot 3: isolated AVD + per-session ADB endpoint
  |
Authenticated streaming gateway
  |-- Browser screen stream
  |-- Touch / keyboard input
  |-- APK install / launch / stop
  |-- Logcat / screenshot
```

The browser must never receive ADB access, emulator console ports, the host-agent token, filesystem paths, or worker credentials. Cloudflare Tunnel is ingress transport, not an authentication or tenant-isolation layer.

## GitHub implementation phases

### Phase 0 — baseline and host qualification

- Preserve the existing local Windows workflow.
- Add a read-only host diagnostic command and documented capacity test.
- Confirm WHPX acceleration and verify the three-slot memory budget.
- Define session states: queued, starting, ready, stopping, stopped, failed, expired.
- Define a hard cap of three active sessions and one session per account for the initial beta.

**Exit criteria:** host diagnostic JSON plus idle/one/two/three-AVD CSV reports are reviewed; acceleration is usable; no public endpoint is exposed yet.

### Phase 1 — security foundation

- Add database migrations and models for users, sessions, uploads, artifacts, quotas and audit events.
- Add secure registration, email verification, login, logout, password reset and session revocation.
- Store password hashes with Argon2id; never store plaintext passwords.
- Use secure, HttpOnly, SameSite cookies, CSRF protection for state-changing browser requests, strict CORS and security headers.
- Add role-based admin authorization.
- Scope every API route, upload, artifact, WebSocket and session action to the authenticated owner.
- Replace the single global settings object with per-session settings.
- Remove public access to destructive global controls such as wipe-all, kill-emulator and global queue operations.

**Exit criteria:** automated tests prove user A cannot read, change, stop, stream, download or control user B's session.

### Phase 2 — session manager and worker isolation

- Implement a Windows session supervisor as the only component allowed to start/stop emulator processes.
- Allocate one AVD slot per session; use a strict maximum of three active slots.
- Use a unique AVD data directory, ADB server/port mapping, temporary directory and artifact directory per session.
- Validate all AVD identifiers against an allowlist; never pass browser-supplied shell commands or paths to subprocesses.
- Apply process timeouts, resource caps where available, cleanup on disconnect and crash recovery.
- Reset emulator state between users; do not reuse a dirty AVD without a verified wipe/reset.
- Keep supervisor IPC bound to localhost or a private named pipe and authenticate control-plane requests.

**Exit criteria:** concurrent sessions cannot inspect or control each other's ADB devices, data, uploads or emulator processes.

### Phase 3 — browser streaming and controls

- Evaluate the Android Emulator WebRTC integration and compatible browser client.
- Build a short-lived, single-session streaming ticket; never expose a shared streaming password.
- Add screen streaming, touch, keyboard, rotation and reconnect handling.
- Add upload progress, APK install status, app launch/stop, Logcat tail and screenshots.
- Test Cloudflare-proxied WebSockets and WebRTC media paths from external networks. If direct WebRTC media is blocked, evaluate a TURN relay and its bandwidth/cost implications before claiming worldwide compatibility.

**Exit criteria:** a remote browser can operate an allocated emulator without direct access to ADB or emulator ports.

### Phase 4 — public registration and abuse controls

- Public registration is allowed, but all device operations require a verified account.
- Initial limits: one active session per account, three active sessions globally, 30-minute session maximum, idle timeout, upload-size cap and per-account/IP rate limits.
- Queue users when all three slots are occupied; return a clear wait estimate only when measured.
- Validate APK extension, MIME type, file signature and size; store outside the web root with random server-side names.
- Add malware scanning where feasible, retention/deletion policy, acceptable-use policy and privacy notice.
- Add abuse reporting, account suspension, IP throttling and admin kill switch.
- Do not permit arbitrary shell, arbitrary ADB commands, host filesystem browsing or arbitrary network tunnelling.

**Exit criteria:** quota, upload, session-expiry, signup abuse and cross-tenant tests pass.

### Phase 5 — deployment and operations

- Run FastAPI, database, session supervisor and stream gateway as separate least-privilege services where practical.
- Install Cloudflare Tunnel as a Windows service and route only the public HTTPS hostname to the reverse proxy.
- Do not port-forward the router; do not expose ADB (5554/5555), emulator console, supervisor or database ports.
- Enable Cloudflare WebSockets in the zone settings and verify long-lived connections.
- Store secrets in Windows protected storage or a dedicated secret manager; keep them out of Git and logs.
- Add health checks, structured logs, metrics, disk cleanup, database backups and alerting.
- Configure Windows power settings, automatic restart, service recovery, update windows and UPS if available.
- Add a public status page and clear maintenance mode.

**Exit criteria:** reboot recovery, tunnel outage, worker crash, backup restore, disk-full and network-loss drills pass.

### Phase 6 — staged launch

1. Owner-only local testing.
2. Invite-only test with trusted developers.
3. Public registration with hard quotas and manual abuse review.
4. Increase concurrency only after measured capacity and stability justify it.

## Initial resource policy

| Resource | Initial policy |
|---|---|
| Concurrent emulators | 3 maximum, only after host qualification |
| Sessions per account | 1 |
| Session duration | 30 minutes maximum |
| Idle timeout | 5 minutes initially |
| APK upload | 100 MB initial cap; configurable by admin |
| Session data | Ephemeral; wipe between users |
| Queue | FIFO, bounded queue |
| Browser privileges | Session-scoped actions only |
| Host access | Never exposed to public users |

These are conservative starting values, not promises of performance. Tune them after load testing.

## Cloudflare and domain checklist

- Create DNS hostname `androidportal.blueflute.in` in the Cloudflare-managed zone.
- Create a Cloudflare Tunnel and install its connector on the Windows host.
- Route the hostname to a local reverse proxy; do not point public traffic directly at the agent.
- Enable WebSockets in Cloudflare Network settings.
- Configure rate limits and WAF rules for login, signup, upload and session creation.
- Test browser streaming from mobile networks and networks behind restrictive NAT.
- Remember: Tunnel can publish HTTP(S) ingress without inbound router ports, but it does not make the Windows PC highly available and does not automatically relay WebRTC media.

## Required GitHub changes by phase

| Order | Paths | Purpose |
|---|---|---|
| 1 | `portal/app/security.py`, `portal/app/auth.py` | Authentication, cookies, CSRF, authorization dependencies |
| 2 | `portal/app/models.py`, `portal/app/migrations/` | Users, sessions, quotas, artifacts, audit records |
| 3 | `portal/app/session_manager.py` | Session lifecycle and slot allocator |
| 4 | `host-agent/supervisor.py` | Private, allowlisted emulator process control |
| 5 | `portal/app/routes/sessions.py` | Owner-scoped session APIs |
| 6 | `portal/app/routes/stream.py` | Short-lived stream tickets and signaling |
| 7 | `portal/app/routes/uploads.py` | Validated per-user APK upload and cleanup |
| 8 | `portal/web/` | Public landing, signup/login, user dashboard, device console |
| 9 | `deploy/cloudflared/` | Example tunnel config and Windows service guide (no tokens) |
| 10 | `tests/security/`, `tests/sessions/` | Tenant-isolation, quota, upload and cleanup tests |

## Non-negotiable security rules

- Never deploy the current single-operator API as a public multi-user app.
- Never trust user IDs, session IDs, file paths, device IDs or commands supplied by the browser.
- Never expose the host-agent token or ADB/emulator ports to browsers or the public internet.
- Never run user-provided APKs with access to the host filesystem, host credentials or other users' sessions.
- Never launch a public beta without cross-tenant authorization tests and resource quotas.
- Keep the existing Windows local mode working throughout the migration.

## References

- [Android Emulator hardware acceleration](https://developer.android.com/studio/run/emulator-acceleration)
- [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/)
- [Cloudflare WebSockets](https://developers.cloudflare.com/network/websockets/)
- [Android Emulator container scripts](https://github.com/google/android-emulator-container-scripts)
- [Android Emulator WebRTC](https://github.com/google/android-emulator-webrtc)

---
Maintained by [BlueFluteX](https://blueflutex.in/) · Code with Harmony
