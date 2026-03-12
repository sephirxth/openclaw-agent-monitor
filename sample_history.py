#!/usr/bin/env python3
from server import load_config, resolve_group_bindings, summarize_agent, maybe_append_history

cfg = load_config()
bindings = resolve_group_bindings(cfg)
agents = [summarize_agent(agent, bindings) for agent in cfg.get('agents', {}).get('list', [])]
maybe_append_history(agents)
print('ok')
