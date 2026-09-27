# Token Usage Tracker

A background service that tracks token usage across AI coding tools and displays it on a local web dashboard.

## Design

Styled after [V–A–C Sreda](design.md) — white-cube exhibition diagram aesthetic. Pure black ink on paper-white field, dotted connectors, no cards, no shadows, no radius.

## Data Sources

| Source | Location |
|---|---|
| OpenCode | `~/.local/share/opencode/opencode.db` |
| VS Code | `~/.config/Code/User/globalStorage/emptyWindowChatSessions/` |
| Antigravity | `~/.config/Antigravity IDE/logs/` |
| Codex | `~/.codex/sessions/` |

## Install

```bash
mkdir -p ~/.local/share/token-tracker
cp tracker.py ~/.local/share/token-tracker/
chmod +x ~/.local/share/token-tracker/tracker.py
```

## Run

```bash
python3 ~/.local/share/token-tracker/tracker.py
```

Open http://127.0.0.1:8765

## Auto-start (systemd)

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

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/stats` | Full JSON stats |
| `GET /api/sessions` | Sessions list |
| `GET /health` | Health check |

## License

MIT
