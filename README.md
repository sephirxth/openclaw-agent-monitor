# OpenClaw Agent Monitor

A lightweight FastAPI + static HTML dashboard for watching OpenClaw subagents in one place.

It reads OpenClaw session telemetry from the gateway and local session files, then renders:

- current agent status
- live token activity
- 5-second realtime pulse feedback
- 1h / 1d / 1w historical token consumption
- daily usage summaries
- task / blocker / next-step context

## Why This Exists

The built-in OpenClaw dashboard is useful as a control plane. This project is optimized for a different question:

> Is each agent actually working right now, or is it idle / hung / waiting?

That is why the UI emphasizes token movement, recent activity, and operator-friendly feedback instead of generic session management.

## When to Use This

- You run OpenClaw agents and need a quick visual check on whether each agent is active, idle, or stuck
- You want real-time token consumption tracking across multiple agents
- You need an operator-friendly dashboard focused on activity status rather than session management
- You are debugging agent behavior and need to see live tool calls and message flow

## Project Layout

```text
agent-monitor/
├── index.html
├── server.py
├── start.sh
├── sample_history.py
├── requirements.txt
├── chat_labels.example.json
└── README.md
```

Local runtime files are intentionally not tracked:

- `chat_labels.json`
- `token_history.jsonl`
- `logs/`

## Requirements

- Python 3.11+
- `openclaw` CLI available on `PATH`
- An OpenClaw workspace with gateway/session data
- Node-installed `json5` from the OpenClaw install, or an override via env var

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

The server is environment-aware and defaults to a local OpenClaw install under `~/.openclaw`.

Supported environment variables:

- `OPENCLAW_ROOT`
  - Default: `~/.openclaw`
- `OPENCLAW_CONFIG`
  - Default: `$OPENCLAW_ROOT/openclaw.json`
- `OPENCLAW_JSON5_PATH`
  - Default: `/home/youyuan/.npm-global/lib/node_modules/openclaw/node_modules/json5`
- `HOST`
  - Default: `0.0.0.0`
- `PORT`
  - Default: `8091`

Optional chat label mapping:

1. Copy `chat_labels.example.json` to `chat_labels.json`
2. Fill in chat IDs you want rendered as friendly names

## Run

Start in foreground:

```bash
python server.py
```

Start in background:

```bash
./start.sh
```

Open:

```text
http://127.0.0.1:8091
```

## Historical Sampling

The dashboard keeps historical token snapshots in `token_history.jsonl`.

If you want history to accumulate even when nobody has the page open, run:

```bash
python sample_history.py
```

on a 1-minute cron.

## Notes

- This project is intentionally simple: one Python server and one static HTML page.
- The UI is tuned for operational feedback, not visual polish alone.
- Some data quality depends on how completely OpenClaw session metadata is persisted in your environment.

## Related Projects

| Project | What It Does |
|---------|-------------|
| [OpenClaw Canvas](https://github.com/sephirxth/openclaw-canvas) | Full infinite-canvas dashboard — if you need task assignment, TODO management, and spatial layout on top of monitoring |
| [WitMani Game Animator](https://github.com/sephirxth/WitMani-game-animator) | AI sprite sheet generator — Claude Code plugin for game character animations |
| [LLM Code Test](https://github.com/sephirxth/LLM_code_test) | Benchmark comparing Claude, Gemini, DeepSeek, Grok on code generation |

[All projects →](https://github.com/sephirxth)
