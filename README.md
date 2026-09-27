# Token Usage Tracker

A background service that tracks token usage across AI coding tools and displays it on a local web dashboard. Built for Linux, macOS, and Windows.

## Design

Styled after V–A–C Sreda — white-cube exhibition diagram aesthetic. Pure black ink on paper-white field, dotted connectors, no cards, no shadows, no radius.

## Features

- **Token tracking** — input, output, reasoning, cache read/write, cost per session
- **Per-session view** — title, path, model, agent, tokens, cost
- **Breakdowns** — by model, by agent, by day
- **Visualizations** — daily bar chart, token composition stacked bar, activity heatmap, model/agent comparison bars
- **Agent process scanner** — find orphaned dev servers, notebooks, and scripts left running by AI agents (cross-platform)
- **Multi-source** — OpenCode, VS Code, Antigravity, Codex
- **Auto-start** — systemd user service on boot
- **Non-blocking** — runs on port 8765, doesn't interfere with other apps

## Data Sources

| Source | Location |
|---|---|
| OpenCode | `~/.local/share/opencode/opencode.db` |
| VS Code | `~/.config/Code/User/globalStorage/emptyWindowChatSessions/` |
| Antigravity | `~/.config/Antigravity IDE/logs/` |
| Codex | `~/.codex/sessions/` |

## Install

```bash
# Clone
git clone https://github.com/Suhas-Koheda/token-tracker.git
cd token-tracker

# Copy service file
mkdir -p ~/.local/share/token-tracker
cp tracker.py ~/.local/share/token-tracker/
chmod +x ~/.local/share/token-tracker/tracker.py
```

## Run

```bash
python3 ~/.local/share/token-tracker/tracker.py
```

Open http://127.0.0.1:8765

## Auto-start (systemd — Linux)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/token-tracker.service << 'EOF'
[Unit]
Description=Token Usage Tracker
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/token-tracker/tracker.py
Restart=on-failure
RestartSec=3
Environment=TOKEN_TRACKER_PORT=8765

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now token-tracker
```

## Auto-start (macOS — launchd)

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.token-tracker.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.token-tracker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>USER_HOME/.local/share/token-tracker/tracker.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TOKEN_TRACKER_PORT</key>
        <string>8765</string>
        <key>HOME</key>
        <string>USER_HOME</string>
    </dict>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.token-tracker.plist
```

## Auto-start (Windows — Task Scheduler)

```powershell
$action = New-ScheduledTaskAction -Execute "python3" -Argument "$env:USERPROFILE\.local\share\token-tracker\tracker.py"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "TokenTracker" -Action $action -Trigger $trigger -Settings $settings
```

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/stats` | Full JSON stats |
| `GET /api/sessions` | Sessions list |
| `GET /api/processes` | Running agent processes |
| `GET /health` | Health check |

## Dependencies

- Python 3.10+
- `psutil` (for process scanner) — `pip install psutil`

## License

MIT
