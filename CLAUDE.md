# CLAUDE.md — Mediacaster Project Context

## What This Is
Mediacaster is a web-based MPEG-TS multicast playout system. Users upload media (video, image, audio), build playlists, and stream them as UDP multicast. It also supports browser source capture — a virtual Chromium instance (with SwiftShader software WebGL) whose display is captured and streamed as multicast.

## Architecture

### Backend (Python/FastAPI)
- **Entry:** `backend/main.py` — FastAPI app, lifespan (startup/shutdown), DB migrations, default admin seeding
- **Config:** `backend/config.py` — centralized config with `MCS_*` env var overrides
- **Database:** `backend/database.py` — SQLAlchemy engine, PostgreSQL (via psycopg2)
- **Models:** `backend/models.py` — User, Asset (VIDEO/IMAGE/AUDIO), Folder (nested directories with sharing), Stream (PLAYLIST/BROWSER source types), StreamItem, BrowserSource, UserStreamAssignment, ServerSetting
- **Auth:** `backend/auth.py` — JWT + bcrypt (pinned `bcrypt<4.1` for passlib compatibility)
- **Routes:**
  - `backend/routes/auth.py` — login, password change, user CRUD (admin)
  - `backend/routes/assets.py` — upload (ownership-filtered), rename, storage endpoint, thumbnails (no auth), folder filtering, search/sort
  - `backend/routes/folders.py` — nested folder CRUD, sharing (admin-only toggle, read-only/read-write), move assets between folders
  - `backend/routes/streams.py` — RBAC: admin creates/configures, assigned users manage playlists. Browser source config, start/stop dispatches to stream_manager or browser_manager
  - `backend/routes/settings.py` — 13 runtime-adjustable server settings, monitoring endpoint with per-stream resource breakdown
- **Services:**
  - `backend/services/transcoder.py` — normalizes all uploads to H.264/AAC. Video, image (black+duration), and audio (black video + source audio). Progress tracking via ffmpeg `-progress pipe:1`
  - `backend/services/stream_manager.py` — manages ffmpeg concat→multicast subprocesses for playlist streams, auto-restart on crash
  - `backend/services/wayland_manager.py` — manages native Wayland capture pipelines for browser/presentation sources. Each source runs cage headless + Chromium/LibreOffice + wf-recorder + ffmpeg + wayvnc + websockify as native processes (no containers)
  - `backend/services/browser_manager.py` — DEPRECATED: old Podman container-based approach, replaced by wayland_manager.py
  - `backend/services/monitor.py` — psutil-based CPU/RAM/network monitoring, per-PID stats

### Frontend (React SPA)
- **Build tool:** Vite (migrated from Create React App)
- **Entry:** `frontend/src/App.jsx` — auth state, forced password change flow
- **API client:** `frontend/src/api.js` — JWT-authenticated fetch wrapper, all endpoints
- **Components:**
  - `Layout.jsx` — tabbed nav (Dashboard/Monitoring/Settings), RBAC-aware
  - `AssetLibrary.jsx` — drag-drop upload, storage bar, transcode progress, inline rename
  - `StreamPanel.jsx` — playlist and browser source types, noVNC iframe, transport controls
  - `Settings.jsx` — user management (admin), server settings (admin), account (all)
  - `Monitoring.jsx` — live bar meters, capacity cards, per-stream breakdown
  - `Login.jsx`, `ChangePassword.jsx`
- **Styles:** `frontend/src/styles/app.css` — dark broadcast engineering aesthetic

### Browser/Presentation Source Capture (Native Wayland Pipeline)
- **Manager:** `backend/services/wayland_manager.py` — manages native Wayland process groups per stream (replaces container-based approach)
- **Pipeline:** cage headless → Chromium (SwiftShader)/LibreOffice → wf-recorder (screencopy) → ffmpeg (encode) → MPEG-TS UDP multicast
- **VNC Preview:** wayvnc + websockify → noVNC iframe in the UI
- **Input:** ydotool for presentation slide control (replaces xdotool)
- **Legacy:** `container/` directory contains the old Podman-based entrypoint and Containerfile (deprecated)
- wf-recorder and ydotool are source-built (not available as RPMs on AL10)

### Infrastructure
- **deploy.sh** — full AlmaLinux deployment (repos, packages, Wayland stack, wf-recorder/ydotool source builds, venv, frontend build, systemd, nginx, firewall, SELinux, multicast routing)
- **nginx/multicast-streamer.conf** — reverse proxy for API + static frontend. noVNC iframe connects directly to the websockify port (6080-6180 range opened in firewall).
- **systemd/multicast-streamer.service** — runs as `mcs` user, `ExecStartPre=+` for /run/user creation, ReadWritePaths includes /run/user for Wayland sockets
- **requirements.txt** — fastapi, uvicorn, sqlalchemy, alembic, psycopg2-binary, python-jose, passlib, bcrypt<4.1, python-multipart, aiofiles, psutil

## RBAC Model
| Action | Admin | Regular User |
|---|---|---|
| Upload content | ✓ | ✓ (own assets only visible) |
| Create folders | ✓ | ✓ (own folders) |
| View folders | ✓ (all) | Own + shared |
| Set folder sharing | ✓ | ✗ |
| Create/configure streams | ✓ | ✗ |
| Modify playlists | ✓ (all) | Only assigned channels |
| Start/stop streams | ✓ (all) | Only assigned channels |
| Monitoring/Settings/Users | ✓ | ✗ |

## Deployment Target
- **OS:** AlmaLinux 10 (also supports 8 and 9)
- **Server IP:** 10.193.1.115 (current dev instance)
- **Service account:** `mcs`
- **App directory:** `/opt/multicast-streamer/`
- **Wayland stack:** weston (headless), wayvnc, wf-recorder (source-built), ydotool (source-built), websockify
- **SELinux:** enforcing — requires `httpd_can_network_connect=1`, port labeling for 6080-6180 (noVNC) and 5950-6050 (VNC)
- **Firewall:** ports 80, 443, 6080-6180/tcp, 5950-6050/tcp open. Multicast 239.0.0.0/8 allowed.

## Known Issues / TODO

### Resolved (2026-03-17)
1. ~~**Browser source multicast output not reaching receivers**~~ — Fixed. Root causes: (a) `ip` binary is in `/usr/sbin/` which wasn't in the systemd service PATH, causing multicast interface detection to fail silently; (b) ffmpeg had no explicit `localaddr=` binding so packets went to the wrong interface. Fix: browser_manager.py now uses `/usr/sbin/ip` to detect the host NIC IP and passes `MULTICAST_IFACE_ADDR` to the container. Also requires `ip route add 239.0.0.0/8 dev <iface>` on the host (deploy.sh handles this but it may not persist across reboots).
2. ~~**noVNC preview not scaling to 16:9 in iframe**~~ — Fixed. Created custom `vnc_embed.html` (in container/) with `scaleViewport=true`, dark background, no UI chrome. Added openbox WM to the container image so Firefox reliably fills the Xvfb framebuffer. Added `browser.window.width/height` prefs for correct initial geometry.
7. ~~**Code comments and documentation**~~ — Done. All backend, frontend, and infrastructure files have comprehensive inline comments.
8. ~~**ExecStartPre in systemd**~~ — Fixed. `+` prefix added for root execution.

### Important
9. **Native 1080p rendering** — cage/wlroots 0.18 ignores `WLR_HEADLESS_RESOLUTION` and defaults to 1280x720. Currently upscaling 720→1080 via wf-recorder's scale filter. Investigate cage `--` output mode flags, weston headless, or wlr-output-management protocol to set native 1080p resolution and eliminate the upscale step.

### Resolved (2026-03-17, batch 2)
3. ~~**nginx noVNC proxy**~~ — Removed. Direct websockify port access is the stable solution; the fragile regex proxy block has been deleted from nginx config.
4. ~~**Default nginx server block**~~ — Fixed. deploy.sh now auto-comments out the embedded server block in `/etc/nginx/nginx.conf`.
5. ~~**npm deprecation warning**~~ — Fixed. deploy.sh uses `--include=dev`.
6. ~~**Multicast route persistence**~~ — Fixed. NetworkManager dispatcher script at `/etc/NetworkManager/dispatcher.d/99-multicast-route` adds `239.0.0.0/8 dev $1` on interface up. deploy.sh now installs this automatically.
7. ~~**Migrate CRA to Vite**~~ — Done. Frontend now uses Vite for builds. Output directory changed from `build/` to `dist/`, entry point from `src/index.js` to `src/main.jsx`.

## Code Style Preferences
- **Comments:** All generated code should be well-commented with explanatory inline comments
- **Variable names:** Human-readable, descriptive names (not abbreviated)
- **No hallucination:** Verify assumptions before generating code. If unsure about a system behavior, say so.
- **Python:** Type hints where practical. Logging via `logging.getLogger()`.
- **React:** Functional components with hooks. Tailwind not used — custom CSS in `app.css`.
- **Formatting:** Dark UI aesthetic matching broadcast engineering tools (dark backgrounds, monospaced values, status indicators)

## Key Dependencies / Gotchas
- `bcrypt` must be pinned `<4.1` — passlib 1.7.4 crashes with bcrypt 4.1+
- Thumbnails/previews served without auth (img tags can't send JWT headers)
- Schema migrations handled by Alembic (`alembic/` directory, `alembic.ini`)
- wf-recorder and ydotool are source-built to `/usr/local/bin/` (not available as RPMs)
- wf-recorder 0.6.0 requires two fixes: (1) colorspace patch in deploy.sh (`frame->colorspace = AVCOL_SPC_RGB` in frame-writer.cpp), (2) `-D` flag at runtime to disable damage-based capture (otherwise hangs on static content)
- `deploy.sh` runs as root, service runs as `mcs` user
- The `mcs` system user has `/sbin/nologin` shell — use `sudo -u mcs` for git operations

## SSH to Dev Server
The reliable pattern for remote server operations (avoids ANSI prompt/expect issues):
1. Write a self-contained bash script locally (e.g., `/tmp/myscript.sh`)
2. Upload: `scp /tmp/myscript.sh mfpadmin@10.2.0.15:/tmp/myscript.sh`
3. Run non-interactively: `ssh mfpadmin@10.2.0.15 "echo 'PASSWORD' | sudo -S bash /tmp/myscript.sh 2>&1"`
4. For mcs user operations: `sudo -u mcs bash -c '...'` inside the script
5. Git operations: `sudo -u mcs git -C /opt/multicast-streamer pull origin main`
- Use `expect` for password automation (match `"password:"` prompt)
- Do NOT use interactive `expect "$ "` prompt matching — ANSI escape codes break it
- Do NOT use `sshpass` — unreliable on this target

## Git Workflow
- Remote: https://github.com/RobarePruyn/Mediacaster.git
- Branch: `main`
- Commit messages should be descriptive
- Never commit `node_modules/`, `__pycache__/`, `venv/`, `db/`, `media/`, `uploads/`, `thumbnails/`, `frontend/dist/`
