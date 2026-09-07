#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch grok.com daily automations into data/grok_automations.json."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

KEEP_DAYS = 3
TAIPEI = timezone(timedelta(hours=8))

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(DIR, 'data', 'grok_automations.json')
AUTH_PATH = os.path.join(os.path.expanduser('~'), '.grok', 'auth.json')

TAB_META = {
    # grok.com/automations 清單順序
    '688176cb-44ea-4210-a7f3-371d4989fe2c': {
        'short': 'Semiconductor & AI Stock News Digest', 'icon': '📰', 'order': 1,
    },
    '30521a61-160d-4f05-a952-7d67d5562a2f': {
        'short': 'Global Watch: Geopolitics & Tech Pulse', 'icon': '🌍', 'order': 2,
    },
    '34cd30fa-07fe-4b24-82e4-f21a6bc8aa79': {
        'short': 'Daily US Stock Earnings Update', 'icon': '📈', 'order': 3,
    },
    'e45b516a-8d5e-42c5-b5c0-e9aba09d46e4': {
        'short': '每日國際局勢 新聞摘要', 'icon': '🗞️', 'order': 4,
    },
    'e2749f77-4d5a-49bd-87ed-294e589aba73': {
        'short': '股票表現', 'icon': '📉', 'order': 5,
    },
}

CADENCE_LABEL = {
    'TASK_CADENCE_ONCE_DAILY': '每天',
    'TASK_CADENCE_WEEKDAYS': '週一至週五',
    'TASK_CADENCE_ONCE_WEEKLY': '每週',
    'TASK_CADENCE_ONCE_MONTHLY': '每月',
}

# Seed from recent known runs so the dashboard is not empty if unreadResults is sparse.
FALLBACK_RECENT = {
    '688176cb-44ea-4210-a7f3-371d4989fe2c': [
        {'taskResultId': '988b64a7-ef3f-4178-9cb3-41f7ad72decf', 'title': 'NVDA beats, AI stocks rebound', 'conversationId': 'c9a55439-1280-456c-bfff-ca4e36f81043', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-27T22:04:17.008135+00:00', 'updateTime': '2026-08-27T22:05:36.157370+00:00'},
        {'taskResultId': '46bcfe72-e652-49f6-996b-84bc447a5b94', 'title': 'NVDA beats, AI stocks rebound', 'conversationId': '765c11d0-1c8c-4498-9aae-45f04d7395e9', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-26T22:05:14.393091+00:00', 'updateTime': '2026-08-26T22:06:26.101247+00:00'},
    ],
    '30521a61-160d-4f05-a952-7d67d5562a2f': [
        {'taskResultId': '1fa193a7-a974-4d93-994b-876bdf230406', 'title': 'Hormuz Talks Stall Amid Oil Surge', 'conversationId': 'fef5c4c4-4291-445c-b9dd-65db96df6039', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-27T22:50:12.112660+00:00', 'updateTime': '2026-08-27T22:51:07.140835+00:00'},
        {'taskResultId': 'a55cd4e8-bca1-4752-b63e-d67aaa788106', 'title': 'Mid-East Hormuz crisis escalates', 'conversationId': '7d3d89fd-f762-4914-820f-6c5a2e8b39a9', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-26T22:50:11.753123+00:00', 'updateTime': '2026-08-27T07:49:30.020039+00:00'},
    ],
    '34cd30fa-07fe-4b24-82e4-f21a6bc8aa79': [
        {'taskResultId': '6d62b99c-dcaf-4847-9f06-1345f2ced92d', 'title': 'Nvidia, Salesforce beat AI reports', 'conversationId': '6e77966c-39d1-4542-93c6-51a38f6d8e0a', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-27T22:40:05.384338+00:00', 'updateTime': '2026-08-27T23:55:10.524802+00:00'},
        {'taskResultId': 'bdafd2f6-eff0-439f-8540-090bebb0eb6b', 'title': 'NVIDIA beats with $96B revenue', 'conversationId': 'a9254377-4547-4b0b-a785-7bb941c0c647', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-26T22:40:03.971614+00:00', 'updateTime': '2026-08-26T22:40:39.424437+00:00'},
    ],
    'e45b516a-8d5e-42c5-b5c0-e9aba09d46e4': [
        {'taskResultId': '3089a4e3-9173-4f6c-851d-39e778ccf452', 'title': 'Nepa flood: 390 dead, Iran oil crisis', 'conversationId': '18a6da6e-f905-4553-b930-c19028c71305', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-27T22:20:05.576178+00:00', 'updateTime': '2026-08-27T22:21:01.464367+00:00'},
        {'taskResultId': '501da5ce-243d-424e-8ff8-4fe5599d1afd', 'title': 'Nvidia beats on AI surge', 'conversationId': '6db611f7-0607-45d7-a677-29d247e9efe8', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-26T22:20:14.309838+00:00', 'updateTime': '2026-08-26T22:21:24.571716+00:00'},
    ],
    'e2749f77-4d5a-49bd-87ed-294e589aba73': [
        {'taskResultId': '6944dc0b-bf39-40ab-9e20-79a56a059829', 'title': 'NVDA beats, AI stocks surge', 'conversationId': 'b200e9d7-b4f7-4a0e-96f4-c9b9cbaafab8', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-27T22:32:09.863383+00:00', 'updateTime': '2026-08-27T22:32:41.098598+00:00'},
        {'taskResultId': '4a34cc82-6274-4ccd-9f50-e9cd12bb3783', 'title': 'NVDA surges post-earnings; TAIEX rebounds', 'conversationId': 'ad4fbc83-a6ae-43a7-90c5-7a0dc4ac4f80', 'status': 'TASK_RESULT_SUCCESS', 'createTime': '2026-08-26T22:31:10.975069+00:00', 'updateTime': '2026-08-26T22:32:21.935859+00:00'},
    ],
}


def grok_token():
    with open(AUTH_PATH, encoding='utf-8') as f:
        auth = json.load(f)
    entry = next(iter(auth.values()))
    return entry.get('key') or entry.get('access_token')


def http_json(url, token, timeout=20):
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'User-Agent': 'PortfolioTracker/v13',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _fmt_am(hhmm):
    try:
        hour_s, minute_s = str(hhmm).split(':')[:2]
        hour = int(hour_s)
        minute = int(minute_s)
    except Exception:
        return str(hhmm or '')
    suffix = 'AM' if hour < 12 else 'PM'
    hour12 = hour % 12 or 12
    return f'{hour12}:{minute:02d} {suffix}'


def _schedule_label(cadence_key, time_of_day):
    clock = _fmt_am(time_of_day) if time_of_day else ''
    if cadence_key == 'TASK_CADENCE_ONCE_DAILY':
        return f'每天於 {clock}' if clock else '每天'
    if cadence_key == 'TASK_CADENCE_WEEKDAYS':
        return f'週一至週五於 {clock}' if clock else '週一至週五'
    base = CADENCE_LABEL.get(cadence_key, '排程')
    return f'{base}於 {clock}' if clock else base


def _parse_iso(iso):
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _taipei_date(iso):
    parsed = _parse_iso(iso)
    if not parsed:
        return ''
    return parsed.astimezone(TAIPEI).strftime('%Y-%m-%d')


def _next_run_label(iso):
    parsed = _parse_iso(iso)
    if not parsed:
        return '—'
    if parsed <= datetime.now(timezone.utc):
        return '—'
    return parsed.astimezone(TAIPEI).strftime('%Y/%m/%d %H:%M')


def _history_entry(row):
    if not isinstance(row, dict):
        return None
    date = row.get('date') or _taipei_date(row.get('createTime') or '')
    if not date:
        return None
    return {
        'date': date,
        'taskResultId': row.get('taskResultId') or '',
        'title': row.get('title') or '',
        'conversationId': row.get('conversationId') or '',
        'status': row.get('status') or '',
        'createTime': row.get('createTime') or '',
        'updateTime': row.get('updateTime') or '',
        'url': row.get('url') or '',
        'body': row.get('body') or '',
        'bodySource': row.get('bodySource') or '',
        'bodyError': row.get('bodyError') or '',
        'recovered': bool(row.get('recovered')),
        'recoverySource': row.get('recoverySource') or '',
        'error': row.get('error') or '',
        'errorCode': row.get('errorCode') or '',
    }


def _history_rank(entry):
    """Prefer a successful run over a failed retry on the same Taipei date."""
    status = str(entry.get('status') or '').upper()
    succeeded = status == 'TASK_RESULT_SUCCESS'
    return (
        1 if succeeded else 0,
        1 if entry.get('body') else 0,
        entry.get('createTime') or '',
    )


def _merge_history(rows):
    by_date = {}
    for row in rows:
        entry = _history_entry(row)
        if not entry:
            continue
        date = entry['date']
        prev = by_date.get(date)
        if not prev:
            by_date[date] = entry
            continue
        winner, loser = (entry, prev) if _history_rank(entry) >= _history_rank(prev) else (prev, entry)
        if not winner.get('body') and loser.get('body') and winner.get('status') == loser.get('status'):
            winner['body'] = loser['body']
            winner['bodySource'] = loser.get('bodySource') or ''
        if winner.get('body'):
            # A completed body supersedes a transient error saved by an earlier run.
            winner['bodyError'] = ''
        by_date[date] = winner
    history = [by_date[d] for d in sorted(by_date.keys(), reverse=True)[:KEEP_DAYS]]
    return history


def _result_row(item):
    cid = item.get('conversationId') or ''
    return {
        'taskResultId': item.get('taskResultId') or '',
        'title': item.get('title') or '',
        'conversationId': cid,
        'status': item.get('status') or '',
        'createTime': item.get('createTime') or '',
        'updateTime': item.get('updateTime') or '',
        'url': f'https://grok.com/c/{cid}' if cid else '',
        'error': item.get('error') or '',
        'errorCode': item.get('errorCode') or '',
    }


def _load_cache():
    try:
        with open(OUT_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_payload(payload):
    """Write the dashboard cache atomically so readers never see a partial JSON."""
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    temp_path = OUT_PATH + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, OUT_PATH)


def _browser_result_entry(row, date):
    title = str(row.get('title') or '').strip()
    body = str(row.get('body') or '').strip()
    body = re.sub(r'^運作了\s*\d+\s*s\s*', '', body, count=1)
    url = str(row.get('url') or '').strip()
    time_label = str(row.get('time') or '').strip()
    if not title:
        raise ValueError('browser snapshot contains an empty title')
    if len(body) < 500:
        raise ValueError(f'browser snapshot body is too short for {title}: {len(body)} chars')
    parsed_url = urlparse(url)
    match = re.search(r'/c/([0-9a-f-]{36})', parsed_url.path, re.I)
    if not match or parsed_url.scheme != 'https' or parsed_url.hostname != 'grok.com':
        raise ValueError(f'browser snapshot has an invalid Grok conversation URL for {title}')
    conversation_id = match.group(1)
    result_id = (parse_qs(parsed_url.query).get('rid') or [''])[0]
    try:
        local_time = datetime.strptime(f'{date} {time_label}', '%Y-%m-%d %I:%M %p').replace(tzinfo=TAIPEI)
    except ValueError as exc:
        raise ValueError(f'browser snapshot has an invalid time for {title}: {time_label}') from exc
    created = local_time.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        'date': date,
        'taskResultId': result_id,
        'title': title,
        'conversationId': conversation_id,
        'status': 'TASK_RESULT_SUCCESS',
        'createTime': created,
        'updateTime': created,
        'url': f'https://grok.com/c/{conversation_id}',
        'body': body,
        'bodySource': 'chrome-visible-result',
        'bodyError': '',
        'recovered': False,
        'recoverySource': '',
        'error': '',
        'errorCode': '',
    }


def import_browser_snapshot(snapshot_path):
    """Merge five visible, successful Grok runs into the existing dashboard cache."""
    with open(snapshot_path, encoding='utf-8') as f:
        snapshot = json.load(f)
    cache = _load_cache()
    automations = cache.get('automations') or []
    if len(automations) != len(TAB_META):
        raise ValueError(f'cached automation count must be {len(TAB_META)}, got {len(automations)}')
    date = str(snapshot.get('date') or '').strip()
    today = datetime.now(TAIPEI).strftime('%Y-%m-%d')
    if date != today:
        raise ValueError(f'browser snapshot date must be Taipei today ({today}), got {date or "empty"}')
    rows = snapshot.get('results') or []
    if len(rows) != len(TAB_META):
        raise ValueError(f'browser snapshot must contain {len(TAB_META)} results, got {len(rows)}')
    by_name = {}
    for row in rows:
        name = str(row.get('taskName') or '').strip()
        if not name or name in by_name:
            raise ValueError(f'browser snapshot has an empty or duplicate task name: {name or "empty"}')
        by_name[name] = row
    expected_names = {str(a.get('name') or a.get('short') or '').strip() for a in automations}
    if {a.get('taskId') for a in automations} != set(TAB_META):
        raise ValueError('cached task IDs differ from expected tasks')
    seen_conversations, seen_bodies = set(), set()
    if set(by_name) != expected_names:
        missing = sorted(expected_names - set(by_name))
        extra = sorted(set(by_name) - expected_names)
        raise ValueError(f'browser snapshot task mismatch; missing={missing}, extra={extra}')
    for item in automations:
        name = str(item.get('name') or item.get('short') or '').strip()
        entry = _browser_result_entry(by_name[name], date)
        digest = hashlib.sha256(entry['body'].encode('utf-8')).hexdigest()
        if entry['conversationId'] in seen_conversations or digest in seen_bodies:
            raise ValueError('duplicate conversation/body: navigation may still show the previous result')
        seen_conversations.add(entry['conversationId'])
        seen_bodies.add(digest)
        history = _merge_history(list(item.get('history') or []) + [entry])
        item['history'] = history
        item['recent'] = history
        item['latest'] = history[0]
    cache['generated'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    cache['source'] = 'chrome-visible-results'
    cache['keepDays'] = KEEP_DAYS
    cache['resultFetchErrors'] = {'rest/tasks': 'HTTP 403; recovered from logged-in Chrome visible results'}
    cache['automations'] = automations
    _write_payload(cache)
    return cache


def _fetch_task_results(live, token, limit=10):
    """Fetch full histories; unreadResults omits failed scheduled runs."""
    results = {}
    errors = {}
    for entry in live.get('tasks') or []:
        task = entry.get('task') or {}
        tid = task.get('taskId') or ''
        if not tid:
            continue
        try:
            page = http_json(f'https://grok.com/rest/tasks/results/{tid}?limit={limit}', token)
            results[tid] = page.get('results') or []
        except Exception as exc:
            errors[tid] = str(exc)
    return results, errors


def build_payload(live, cache=None, task_results=None, result_fetch_errors=None):
    cache = cache or {}
    task_results = task_results or {}
    cache_by_id = {a.get('taskId'): a for a in (cache.get('automations') or []) if a.get('taskId')}
    unread = live.get('unreadResults') or []
    by_task = {}
    for item in unread:
        tid = item.get('taskId')
        if not tid:
            continue
        by_task.setdefault(tid, []).append(_result_row(item))
    for tid, rows in task_results.items():
        known = {r.get('taskResultId') for r in by_task.get(tid, []) if r.get('taskResultId')}
        for item in rows or []:
            rid = item.get('taskResultId')
            if rid and rid in known:
                continue
            by_task.setdefault(tid, []).append(_result_row(item))
            if rid:
                known.add(rid)

    automations = []
    for entry in live.get('tasks') or []:
        task = entry.get('task') or {}
        tid = task.get('taskId') or ''
        meta = TAB_META.get(tid, {})
        daily = None
        for sch in entry.get('schedules') or []:
            if sch.get('taskCadence') == 'TASK_CADENCE_ONCE_DAILY' and sch.get('isEnabled'):
                daily = sch
                break
        if daily is None:
            enabled = [s for s in (entry.get('schedules') or []) if s.get('isEnabled')]
            daily = enabled[0] if enabled else ((entry.get('schedules') or [None])[0])

        recent = by_task.get(tid) or []
        cached = cache_by_id.get(tid) or {}
        seen = {r.get('taskResultId') for r in recent if r.get('taskResultId')}
        for old in list(cached.get('recent') or []) + [_result_row(x) for x in (FALLBACK_RECENT.get(tid) or [])]:
            oid = old.get('taskResultId')
            if oid and oid not in seen:
                recent.append(old)
                seen.add(oid)
        recent.sort(key=lambda r: r.get('createTime') or '', reverse=True)
        history_src = []
        history_src.extend(cached.get('history') or [])
        if cached.get('latest'):
            history_src.append(cached.get('latest'))
        history_src.extend(recent)
        history_src.extend(cached.get('recent') or [])
        history = _merge_history(history_src)
        recent = history
        latest = history[0] if history else (cached.get('latest') or None)
        cadence_key = (daily or {}).get('taskCadence') or ''
        time_of_day = (daily or {}).get('timeOfDay') or ''
        is_active = bool(task.get('isActive'))
        automations.append({
            'taskId': tid,
            'name': task.get('name') or meta.get('short') or '未命名',
            'short': meta.get('short') or (task.get('name') or '自動化'),
            'icon': meta.get('icon') or '⚡',
            'order': meta.get('order') or 99,
            'prompt': task.get('prompt') or '',
            'isActive': is_active,
            'statusLabel': '啟用中' if is_active else '已暫停',
            'modelMode': task.get('modelMode') or '',
            'timeOfDay': time_of_day,
            'timezone': (daily or {}).get('timezone') or 'Asia/Taipei',
            'cadence': CADENCE_LABEL.get(cadence_key, '排程'),
            'scheduleLabel': _schedule_label(cadence_key, time_of_day),
            'nextRun': (daily or {}).get('nextRun') or '',
            'nextRunLabel': _next_run_label((daily or {}).get('nextRun') or ''),
            'grokUrl': f'https://grok.com/automations?automationId={tid}' if tid else 'https://grok.com/automations',
            'latest': latest,
            'history': history,
            'recent': recent,
        })

    automations.sort(key=lambda a: (a.get('order') or 99, a.get('timeOfDay') or ''))
    return {
        'generated': __import__('datetime').datetime.now(
            __import__('datetime').timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'live-results',
        'keepDays': KEEP_DAYS,
        'resultFetchErrors': result_fetch_errors or {},
        'automations': automations,
    }


def _extract_response_text(payload):
    if not isinstance(payload, dict):
        return ''
    if payload.get('output_text'):
        return str(payload['output_text']).strip()
    chunks = []
    for item in payload.get('output') or []:
        if not isinstance(item, dict):
            continue
        kind = item.get('type') or ''
        if kind in ('reasoning', 'web_search_call', 'x_search_call', 'code_interpreter_call'):
            continue
        content = item.get('content')
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get('text') or part.get('output_text') or ''
                    if text and part.get('type') not in ('reasoning',):
                        chunks.append(str(text).strip())
                elif isinstance(part, str) and part.strip():
                    chunks.append(part.strip())
        elif item.get('text') and kind not in ('summary_text',):
            chunks.append(str(item['text']).strip())
    return '\n\n'.join(c for c in chunks if c).strip()


def fill_mail_bodies(payload, token, force=False):
    """Render each automation prompt into latest.body for on-page display."""
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'PortfolioTracker/v13',
    }
    for item in payload.get('automations') or []:
        latest = item.get('latest')
        if not isinstance(latest, dict):
            latest = {}
            item['latest'] = latest
        status = str(latest.get('status') or '').upper()
        recoverable_failure = (
            status == 'TASK_RESULT_ERROR'
            and str(latest.get('errorCode') or '').upper() == 'USAGE_POOL_EXHAUSTED'
        )
        if status != 'TASK_RESULT_SUCCESS' and not recoverable_failure:
            continue
        if latest.get('body') and not force:
            latest['bodyError'] = ''
            continue
        prompt = (item.get('prompt') or '').strip()
        if not prompt:
            continue
        name = item.get('name') or item.get('short') or '自動化'
        user_msg = (
            f'你正在產出 Grok 每日自動信「{name}」的完整正文。\n'
            f'請直接寫信，不要開場白、不要說你是 AI。使用繁體中文。\n\n{prompt}'
        )
        body = {
            'model': 'grok-4-latest',
            'input': [{'role': 'user', 'content': user_msg}],
            'tools': [{'type': 'web_search'}],
        }
        last_error = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    'https://api.x.ai/v1/responses',
                    data=json.dumps(body).encode('utf-8'),
                    headers=headers,
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                text = _extract_response_text(data)
                if not text:
                    raise RuntimeError('正文服務回傳空內容')
                latest['body'] = text
                latest['bodySource'] = 'xai-recovery' if recoverable_failure else 'xai-responses'
                latest['bodyError'] = ''
                if recoverable_failure:
                    latest['recovered'] = True
                    latest['recoverySource'] = 'xai-responses'
                latest['date'] = latest.get('date') or _taipei_date(latest.get('createTime') or '') or datetime.now(TAIPEI).strftime('%Y-%m-%d')
                item['history'] = _merge_history(list(item.get('history') or []) + [latest])
                item['recent'] = item['history']
                item['latest'] = item['history'][0] if item.get('history') else latest
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        if last_error is not None and not latest.get('body'):
            latest['bodyError'] = str(last_error)
    return payload


def fetch_and_save(fill_bodies=False, force_bodies=False):
    token = grok_token()
    live = http_json('https://grok.com/rest/tasks', token)
    task_results, result_errors = _fetch_task_results(live, token)
    payload = build_payload(live, _load_cache(), task_results, result_errors)
    # Only publish actual saved results. Never generate replacement articles and
    # label them as the original scheduled result.
    today = datetime.now(TAIPEI).strftime('%Y-%m-%d')
    rows = payload.get('automations') or []
    if {a.get('taskId') for a in rows} != set(TAB_META) or len(rows) != len(TAB_META):
        raise ValueError('live task set is incomplete; preserve cache and use Chrome')
    for item in rows:
        latest = item.get('latest') or {}
        if (latest.get('date') != today or latest.get('status') != 'TASK_RESULT_SUCCESS'
                or len(latest.get('body') or '') < 500):
            raise ValueError('live results lack complete current-day bodies; preserve cache and use Chrome')
    _write_payload(payload)
    return payload


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    if len(sys.argv) == 3 and sys.argv[1] == '--browser-snapshot':
        data = import_browser_snapshot(sys.argv[2])
    elif len(sys.argv) == 1:
        data = fetch_and_save(fill_bodies=True)
    else:
        raise SystemExit('usage: fetch_grok_automations.py [--browser-snapshot SNAPSHOT.json]')
    print('saved', OUT_PATH, 'count', len(data.get('automations') or []))
    for a in data['automations']:
        latest = a.get('latest') or {}
        n = len(latest.get('body') or '')
        print(f"- {a.get('icon','')} {a.get('name')} | {latest.get('title','(尚無結果)')} | body={n} chars")
