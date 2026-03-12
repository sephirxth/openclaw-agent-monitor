#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

APP = FastAPI(title="Luna Agent Monitor")
PROJECT_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/home/youyuan/.openclaw")).expanduser()
WORKSPACE = ROOT / "workspace"
CONFIG_PATH = Path(os.environ.get("OPENCLAW_CONFIG", str(ROOT / "openclaw.json"))).expanduser()
JSON5_PATH = Path(
    os.environ.get(
        "OPENCLAW_JSON5_PATH",
        "/home/youyuan/.npm-global/lib/node_modules/openclaw/node_modules/json5",
    )
).expanduser()
CHAT_LABELS_PATH = PROJECT_DIR / "chat_labels.json"
HISTORY_PATH = PROJECT_DIR / "token_history.jsonl"
PREV_TOKEN_SNAPSHOTS: dict[str, dict[str, int]] = {}
LAST_HISTORY_WRITE: dict[str, int] = {}
HISTORY_WRITE_MIN_INTERVAL_MS = 60 * 1000
GATEWAY_SESSIONS_CACHE: dict[str, Any] = {'ts': 0, 'sessions': []}
GATEWAY_CACHE_TTL_MS = 5 * 1000
USAGE_SUMMARY_CACHE: dict[str, Any] = {}
USAGE_CACHE_TTL_MS = 60 * 1000
LOCAL_TZ = ZoneInfo('Asia/Shanghai')


def strip_jsonc(text: str) -> str:
    text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def load_config() -> dict[str, Any]:
    """Parse OpenClaw's JSON5-like config via Node's bundled json5 parser."""
    node_code = f"""
const fs = require('fs');
const JSON5 = require('{JSON5_PATH.as_posix()}');
const raw = fs.readFileSync('{CONFIG_PATH.as_posix()}', 'utf8');
const obj = JSON5.parse(raw);
process.stdout.write(JSON.stringify(obj));
"""
    out = subprocess.check_output(['node', '-e', node_code], text=True, timeout=5)
    return json.loads(out)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return ""


def parse_first_unfinished_todo(path: Path) -> str | None:
    text = read_text(path)
    for line in text.splitlines():
        m = re.match(r'^\s*(?:-|\d+\.)\s*\[([ /x])\]\s+(.+)$', line)
        if m and m.group(1) != 'x':
            return re.sub(r'`[^`]+`', '', m.group(2)).strip()
    return None


def parse_active_task(path: Path) -> str | None:
    text = read_text(path)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('你当前唯一优先任务：'):
            return line.replace('你当前唯一优先任务：', '').strip('**。 ')
        if line.startswith('- [/]') or line.startswith('- [ ]'):
            return line
    return None


def latest_relevant_file(workspace: Path) -> dict[str, Any] | None:
    ignore_parts = {'.git', '.openclaw', 'memory', '__pycache__'}
    candidates = []
    for p in workspace.rglob('*'):
        if not p.is_file():
            continue
        if any(part in ignore_parts for part in p.parts):
            continue
        if p.name in {'IDENTITY.md', 'SOUL.md', 'USER.md', 'MEMORY.md', 'AGENTS.md', 'TOOLS.md', 'HEARTBEAT.md', 'STATUS.json'}:
            continue
        candidates.append(p)
    if not candidates:
        return None
    p = max(candidates, key=lambda x: x.stat().st_mtime)
    return {
        'path': str(p),
        'mtime': int(p.stat().st_mtime),
        'name': p.name,
    }


def parse_iso_or_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def resolve_group_bindings(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    labels = load_json(CHAT_LABELS_PATH, {})
    result: dict[str, dict[str, str]] = {}
    for item in cfg.get('bindings', []):
        match = item.get('match', {})
        peer = match.get('peer', {})
        if match.get('channel') == 'feishu' and peer.get('kind') == 'group':
            cid = peer.get('id')
            if cid:
                result[item['agentId']] = {
                    'chat_id': cid,
                    'chat_name': labels.get(cid, cid),
                }
    return result


def load_sessions_for(agent_id: str) -> dict[str, Any]:
    path = ROOT / 'agents' / agent_id / 'sessions' / 'sessions.json'
    return load_json(path, {})


def gateway_call(method: str, params: dict[str, Any] | None = None, timeout_ms: int = 5000) -> dict[str, Any] | None:
    try:
        out = subprocess.check_output(
            [
                'openclaw', 'gateway', 'call', method,
                '--json',
                '--params', json.dumps(params or {}, ensure_ascii=False),
                '--timeout', str(timeout_ms),
            ],
            text=True,
            timeout=max(2, timeout_ms // 1000 + 2),
        )
        return json.loads(out)
    except Exception:
        return None


def agent_id_from_session_key(key: str | None) -> str | None:
    if not key:
        return None
    m = re.match(r'^agent:([^:]+):', key)
    return m.group(1) if m else None


def load_gateway_sessions(limit: int = 200) -> list[dict[str, Any]]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cached_ts = int(GATEWAY_SESSIONS_CACHE.get('ts') or 0)
    if now_ms - cached_ts < GATEWAY_CACHE_TTL_MS:
        return list(GATEWAY_SESSIONS_CACHE.get('sessions') or [])
    resp = gateway_call('sessions.list', {'limit': limit}, timeout_ms=5000) or {}
    sessions = list(resp.get('sessions') or [])
    GATEWAY_SESSIONS_CACHE['ts'] = now_ms
    GATEWAY_SESSIONS_CACHE['sessions'] = sessions
    return sessions


def today_local_date() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def aggregate_usage_summary(start_date: str, end_date: str) -> dict[str, Any]:
    cache_key = f'{start_date}:{end_date}'
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cached = USAGE_SUMMARY_CACHE.get(cache_key) or {}
    if now_ms - int(cached.get('ts') or 0) < USAGE_CACHE_TTL_MS:
        return cached.get('data') or {}

    resp = gateway_call('sessions.usage', {'startDate': start_date, 'endDate': end_date}, timeout_ms=15000) or {}
    sessions = list(resp.get('sessions') or [])
    agents: dict[str, Any] = {}
    for sess in sessions:
        key = sess.get('key')
        aid = agent_id_from_session_key(key)
        if not aid:
            continue
        usage = sess.get('usage') or {}
        agent = agents.setdefault(aid, {
            'agent_id': aid,
            'start_date': start_date,
            'end_date': end_date,
            'total_tokens': 0,
            'input': 0,
            'output': 0,
            'cache_read': 0,
            'cache_write': 0,
            'session_count': 0,
            'models_map': {},
            'sessions': [],
        })
        total_tokens = int(usage.get('totalTokens') or 0)
        input_tokens = int(usage.get('input') or 0)
        output_tokens = int(usage.get('output') or 0)
        cache_read = int(usage.get('cacheRead') or 0)
        cache_write = int(usage.get('cacheWrite') or 0)
        agent['total_tokens'] += total_tokens
        agent['input'] += input_tokens
        agent['output'] += output_tokens
        agent['cache_read'] += cache_read
        agent['cache_write'] += cache_write
        agent['session_count'] += 1
        agent['sessions'].append({
            'key': key,
            'updated_at': int(sess.get('updatedAt') or 0),
            'total_tokens': total_tokens,
            'input': input_tokens,
            'output': output_tokens,
            'cache_read': cache_read,
            'cache_write': cache_write,
            'model_provider': sess.get('modelProvider'),
            'model': sess.get('model'),
        })
        for mu in usage.get('modelUsage') or []:
            provider = mu.get('provider') or sess.get('modelProvider') or 'unknown'
            model = mu.get('model') or sess.get('model') or 'unknown'
            totals = mu.get('totals') or {}
            mt = int(totals.get('totalTokens') or 0)
            mi = int(totals.get('input') or 0)
            mo = int(totals.get('output') or 0)
            mcr = int(totals.get('cacheRead') or 0)
            mcw = int(totals.get('cacheWrite') or 0)
            mk = f'{provider}/{model}'
            bucket = agent['models_map'].setdefault(mk, {
                'provider': provider,
                'model': model,
                'total_tokens': 0,
                'input': 0,
                'output': 0,
                'cache_read': 0,
                'cache_write': 0,
            })
            bucket['total_tokens'] += mt
            bucket['input'] += mi
            bucket['output'] += mo
            bucket['cache_read'] += mcr
            bucket['cache_write'] += mcw
        if not (usage.get('modelUsage') or []):
            provider = sess.get('modelProvider') or 'unknown'
            model = sess.get('model') or 'unknown'
            mk = f'{provider}/{model}'
            bucket = agent['models_map'].setdefault(mk, {
                'provider': provider,
                'model': model,
                'total_tokens': 0,
                'input': 0,
                'output': 0,
                'cache_read': 0,
                'cache_write': 0,
            })
            bucket['total_tokens'] += total_tokens
            bucket['input'] += input_tokens
            bucket['output'] += output_tokens
            bucket['cache_read'] += cache_read
            bucket['cache_write'] += cache_write

    for aid, agent in agents.items():
        models = sorted(agent.pop('models_map').values(), key=lambda x: int(x.get('total_tokens') or 0), reverse=True)
        sessions_sorted = sorted(agent['sessions'], key=lambda x: int(x.get('total_tokens') or 0), reverse=True)
        total = max(1, int(agent.get('total_tokens') or 0))
        for row in sessions_sorted:
            row['share_pct'] = round(100.0 * int(row.get('total_tokens') or 0) / total, 1)
        for row in models:
            row['share_pct'] = round(100.0 * int(row.get('total_tokens') or 0) / total, 1)
        agent['models'] = models[:5]
        agent['top_sessions'] = sessions_sorted[:5]
        agent['generated_at'] = now_ms

    data = {
        'generated_at': now_ms,
        'start_date': start_date,
        'end_date': end_date,
        'agents': agents,
    }
    USAGE_SUMMARY_CACHE[cache_key] = {'ts': now_ms, 'data': data}
    return data


def rank_session_for_tokens(meta: dict[str, Any]) -> tuple[int, int, int]:
    updated = int(meta.get('updatedAt') or 0)
    fresh = 1 if meta.get('totalTokensFresh') else 0
    total = int(meta.get('totalTokens') or 0)
    return (fresh, total > 0, updated)


def best_gateway_session_for(agent_id: str) -> dict[str, Any] | None:
    sessions = [s for s in load_gateway_sessions() if agent_id_from_session_key(s.get('key')) == agent_id]
    if not sessions:
        return None
    sessions.sort(key=rank_session_for_tokens, reverse=True)
    return sessions[0]


def freshest_gateway_session_for(agent_id: str) -> dict[str, Any] | None:
    sessions = [s for s in load_gateway_sessions() if agent_id_from_session_key(s.get('key')) == agent_id]
    if not sessions:
        return None
    sessions.sort(key=lambda s: int(s.get('updatedAt') or 0), reverse=True)
    return sessions[0]


def extract_token_stats(session_meta: dict[str, Any] | None) -> dict[str, int]:
    meta = session_meta or {}
    stats = {
        'total': int(meta.get('totalTokens') or 0),
        'input': int(meta.get('inputTokens') or 0),
        'output': int(meta.get('outputTokens') or 0),
        'cache_read': int(meta.get('cacheRead') or 0),
        'cache_write': int(meta.get('cacheWrite') or 0),
        'updated_at': int(meta.get('updatedAt') or 0),
    }
    if stats['total'] > 0:
        return stats

    session_file = meta.get('sessionFile')
    if not session_file:
        return stats

    try:
        lines = Path(session_file).read_text(encoding='utf-8', errors='ignore').splitlines()
        for line in reversed(lines):
            obj = json.loads(line)
            msg = obj.get('message') or {}
            usage = msg.get('usage') or obj.get('usage') or {}
            if usage:
                stats['input'] = int(usage.get('input') or usage.get('input_tokens') or 0)
                stats['output'] = int(usage.get('output') or usage.get('output_tokens') or 0)
                stats['cache_read'] = int(usage.get('cacheRead') or usage.get('cache_read') or 0)
                stats['cache_write'] = int(usage.get('cacheWrite') or usage.get('cache_write') or 0)
                total = usage.get('totalTokens') or usage.get('total') or usage.get('total_tokens')
                if total is None:
                    total = stats['input'] + stats['output'] + stats['cache_read'] + stats['cache_write']
                stats['total'] = int(total or 0)
                stats['updated_at'] = int(meta.get('updatedAt') or 0)
                return stats
        return stats
    except Exception:
        return stats


def compute_token_activity(agent_id: str, token_stats: dict[str, int]) -> dict[str, Any]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    prev = PREV_TOKEN_SNAPSHOTS.get(agent_id)
    PREV_TOKEN_SNAPSHOTS[agent_id] = {**token_stats, 'seen_at': now_ms}
    if not prev:
        return {
            'total': token_stats['total'],
            'input': token_stats['input'],
            'output': token_stats['output'],
            'cache_read': token_stats['cache_read'],
            'cache_write': token_stats['cache_write'],
            'delta_total': 0,
            'delta_input': 0,
            'delta_output': 0,
            'delta_cache_read': 0,
            'delta_cache_write': 0,
            'window_ms': 0,
            'live': False,
        }
    return {
        'total': token_stats['total'],
        'input': token_stats['input'],
        'output': token_stats['output'],
        'cache_read': token_stats['cache_read'],
        'cache_write': token_stats['cache_write'],
        'delta_total': token_stats['total'] - prev.get('total', 0),
        'delta_input': token_stats['input'] - prev.get('input', 0),
        'delta_output': token_stats['output'] - prev.get('output', 0),
        'delta_cache_read': token_stats['cache_read'] - prev.get('cache_read', 0),
        'delta_cache_write': token_stats['cache_write'] - prev.get('cache_write', 0),
        'window_ms': now_ms - prev.get('seen_at', now_ms),
        'live': (token_stats['total'] - prev.get('total', 0)) > 0,
    }


def summarize_agent(agent: dict[str, Any], bindings: dict[str, dict[str, str]]) -> dict[str, Any]:
    aid = agent['id']
    workspace = Path(agent['workspace'])
    status_path = workspace / 'STATUS.json'
    status = load_json(status_path, {})

    freshest_session = freshest_gateway_session_for(aid)
    token_session = best_gateway_session_for(aid)
    telemetry_source = 'gateway.sessions.list'

    if freshest_session is None and token_session is None:
        sessions = load_sessions_for(aid)
        latest_session_key = None
        latest_session = None
        latest_updated = None
        for key, meta in sessions.items():
            updated = meta.get('updatedAt')
            if isinstance(updated, (int, float)) and (latest_updated is None or updated > latest_updated):
                latest_updated = int(updated)
                latest_session_key = key
                latest_session = meta
        freshest_session = latest_session
        token_session = latest_session
        telemetry_source = 'local.sessions.json'
    else:
        latest_session_key = (freshest_session or {}).get('key')
        latest_updated = int((freshest_session or {}).get('updatedAt') or 0) or None

    token_stats = extract_token_stats(token_session)
    token_activity = compute_token_activity(aid, token_stats)
    token_activity['fresh'] = bool((token_session or {}).get('totalTokensFresh'))
    token_activity['session_updated_at'] = int(token_stats.get('updated_at') or 0)
    explicit_status = status.get('status') if isinstance(status, dict) else None
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    recent_token_update = int(token_stats.get('updated_at') or latest_updated or 0)
    recent_token_age_ms = (now_ms - recent_token_update) if recent_token_update else None
    token_activity['updated_age_ms'] = recent_token_age_ms
    if explicit_status:
        computed = explicit_status
    elif recent_token_update and recent_token_age_ms is not None and recent_token_age_ms < 45 * 1000 and (token_activity['fresh'] or token_stats['total'] > 0):
        computed = 'running'
    elif latest_updated and now_ms - latest_updated < 15 * 60 * 1000:
        computed = 'active'
    elif latest_updated and now_ms - latest_updated < 2 * 60 * 60 * 1000:
        computed = 'idle'
    else:
        computed = 'stale'

    task = (status.get('task') if isinstance(status, dict) else None) or parse_first_unfinished_todo(workspace / 'TODO.md') or parse_active_task(workspace / 'ACTIVE_TASK.md')
    artifact = latest_relevant_file(workspace)
    binding = bindings.get(aid)
    return {
        'agent_id': aid,
        'name': agent.get('name') or aid,
        'emoji': (agent.get('identity') or {}).get('emoji'),
        'workspace': str(workspace),
        'status': computed,
        'status_source': 'status.json' if explicit_status else 'session-telemetry',
        'telemetry_source': telemetry_source,
        'task': task,
        'step': status.get('step') if isinstance(status, dict) else None,
        'result': status.get('result') if isinstance(status, dict) else None,
        'blocker': status.get('blocker') if isinstance(status, dict) else None,
        'next_action': status.get('next') if isinstance(status, dict) else None,
        'last_updated_ms': parse_iso_or_ms(status.get('updated_at')) if isinstance(status, dict) else latest_updated,
        'latest_session_key': latest_session_key,
        'latest_session': freshest_session,
        'token_session_key': (token_session or {}).get('key'),
        'token_session': token_session,
        'binding': binding,
        'latest_artifact': artifact,
        'tokens': token_activity,
    }


def tailscale_ip() -> str | None:
    try:
        out = subprocess.check_output(['tailscale', 'ip', '-4'], text=True, timeout=3).strip().splitlines()
        return out[0].strip() if out else None
    except Exception:
        return None


def maybe_append_history(agents: list[dict[str, Any]]) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lines = []
    for a in agents:
        aid = a['agent_id']
        t = a.get('tokens') or {}
        total = int(t.get('total') or 0)
        if total <= 0:
            continue
        last = LAST_HISTORY_WRITE.get(aid, 0)
        if now_ms - last < HISTORY_WRITE_MIN_INTERVAL_MS:
            continue
        entry = {
            'ts': now_ms,
            'agent_id': aid,
            'session_key': a.get('token_session_key') or a.get('latest_session_key'),
            'total': total,
            'input': int(t.get('input') or 0),
            'output': int(t.get('output') or 0),
            'cache_read': int(t.get('cache_read') or 0),
            'cache_write': int(t.get('cache_write') or 0),
            'status': a.get('status'),
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
        LAST_HISTORY_WRITE[aid] = now_ms
    if lines:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')


def load_history(range_key: str = '1h') -> dict[str, list[dict[str, Any]]]:
    if range_key == '1w':
        window_ms = 7 * 24 * 60 * 60 * 1000
    elif range_key == '1d':
        window_ms = 24 * 60 * 60 * 1000
    else:
        window_ms = 60 * 60 * 1000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff = now_ms - window_ms
    raw: dict[str, list[dict[str, Any]]] = {}
    if not HISTORY_PATH.exists():
        return raw
    for line in HISTORY_PATH.read_text(encoding='utf-8', errors='ignore').splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = int(obj.get('ts') or 0)
        if ts < cutoff:
            continue
        aid = obj.get('agent_id')
        if not aid:
            continue
        raw.setdefault(aid, []).append(obj)

    data: dict[str, list[dict[str, Any]]] = {}
    for aid, items in raw.items():
        items.sort(key=lambda x: int(x.get('ts') or 0))
        prev: dict[str, Any] | None = None
        series: list[dict[str, Any]] = []
        for item in items:
            ts = int(item.get('ts') or 0)
            total = int(item.get('total') or 0)
            input_tokens = int(item.get('input') or 0)
            output_tokens = int(item.get('output') or 0)
            cache_read = int(item.get('cache_read') or 0)
            cache_write = int(item.get('cache_write') or 0)
            session_key = item.get('session_key')
            if prev is None:
                delta_total = 0
                delta_input = 0
                delta_output = 0
                delta_cache_read = 0
                delta_cache_write = 0
                interval_ms = 0
            else:
                same_session = bool(session_key and prev.get('session_key') and session_key == prev.get('session_key'))
                monotonic = total >= int(prev.get('total') or 0)
                if monotonic and (same_session or not session_key or not prev.get('session_key')):
                    delta_total = total - int(prev.get('total') or 0)
                    delta_input = input_tokens - int(prev.get('input') or 0)
                    delta_output = output_tokens - int(prev.get('output') or 0)
                    delta_cache_read = cache_read - int(prev.get('cache_read') or 0)
                    delta_cache_write = cache_write - int(prev.get('cache_write') or 0)
                else:
                    delta_total = total
                    delta_input = input_tokens
                    delta_output = output_tokens
                    delta_cache_read = cache_read
                    delta_cache_write = cache_write
                interval_ms = max(0, ts - int(prev.get('ts') or ts))
            series.append({
                'ts': ts,
                'session_key': session_key,
                'total': total,
                'input': input_tokens,
                'output': output_tokens,
                'cache_read': cache_read,
                'cache_write': cache_write,
                'delta_total': max(0, delta_total),
                'delta_input': max(0, delta_input),
                'delta_output': max(0, delta_output),
                'delta_cache_read': max(0, delta_cache_read),
                'delta_cache_write': max(0, delta_cache_write),
                'interval_ms': interval_ms,
            })
            prev = {
                'ts': ts,
                'session_key': session_key,
                'total': total,
                'input': input_tokens,
                'output': output_tokens,
                'cache_read': cache_read,
                'cache_write': cache_write,
            }
        data[aid] = series
    return data


@APP.get('/api/overview')
def api_overview():
    cfg = load_config()
    bindings = resolve_group_bindings(cfg)
    agents = [summarize_agent(agent, bindings) for agent in cfg.get('agents', {}).get('list', [])]
    maybe_append_history(agents)
    return JSONResponse({
        'generated_at': int(datetime.now(timezone.utc).timestamp() * 1000),
        'tailscale_ip': tailscale_ip(),
        'agents': agents,
    })


@APP.get('/api/history')
def api_history(range: str = '1h'):
    data = load_history(range)
    return JSONResponse({
        'generated_at': int(datetime.now(timezone.utc).timestamp() * 1000),
        'range': range,
        'series': data,
    })


@APP.get('/api/usage-summary')
def api_usage_summary(startDate: str | None = None, endDate: str | None = None):
    start_date = startDate or today_local_date()
    end_date = endDate or start_date
    data = aggregate_usage_summary(start_date, end_date)
    return JSONResponse(data)


@APP.get('/', response_class=HTMLResponse)
def index():
    html = (WORKSPACE / 'services/agent-monitor/index.html').read_text(encoding='utf-8')
    return HTMLResponse(html)


if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '8091'))
    uvicorn.run(APP, host=host, port=port, log_level='info')
