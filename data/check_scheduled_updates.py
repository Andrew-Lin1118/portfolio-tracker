"""Read-only freshness gate for daily dashboard automations (exit 1 = repair needed)."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))

def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0

def date_of(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    if parsed > dt.datetime.now(TZ) + dt.timedelta(minutes=5):
        raise ValueError('future timestamp')
    return parsed.astimezone(TZ).date().isoformat()

def inspect(kind, root=ROOT, today=None):
    today = today or dt.datetime.now(TZ).date().isoformat()
    errors = []
    def require(ok, message):
        if not ok:
            errors.append(message)
    def read(name):
        return json.loads((root / 'data' / name).read_text(encoding='utf-8'))
    try:
        if kind == 'grok':
            from fetch_grok_automations import TAB_META
            payload = read('grok_automations.json')
            rows = payload.get('automations', [])
            require(len(rows) == 5 and {r.get('taskId') for r in rows} == set(TAB_META), 'expected five unique Grok tasks')
            require(date_of(payload.get('generated')) == today, 'Grok generated date is stale')
            require(payload.get('source') in ('live-results', 'chrome-visible-results'), 'unverified Grok source')
            bodies = set()
            for row in rows:
                latest = row.get('latest') or {}
                body = latest.get('body') or ''
                require(latest.get('date') == today and date_of(latest.get('createTime')) == today, 'Grok result date is stale')
                require(latest.get('status') == 'TASK_RESULT_SUCCESS' and len(body) >= 500, 'Grok result incomplete')
                require(latest.get('bodySource') not in ('xai-responses', 'xai-recovery'), 'generated substitute is not original result')
                bodies.add(hashlib.sha256(body.encode()).hexdigest())
                require(len(row.get('history', [])) <= 3, 'Grok history exceeds three entries')
            require(len(bodies) == 5, 'duplicate Grok bodies')
        elif kind == 'pionex':
            calendar = read('pionex_grid_calendar.json')
            row = calendar.get('days', {}).get(today, {})
            crypto = read('crypto_balance.json')
            rebalance = read('pionex_smart_rebalance.json')
            require(positive(row.get('pionex_net_twd')), 'today Pionex net worth missing')
            require(date_of(crypto.get('manual_pionex_twd_ts')) == today, 'Pionex Chrome snapshot is stale')
            require(positive(crypto.get('manual_pionex_twd')) and row.get('pionex_net_twd') == crypto.get('manual_pionex_twd'), 'Pionex snapshots disagree')
            require(date_of(rebalance.get('updated_at')) == today, 'rebalance snapshot is stale')
            from update_pionex_smart_rebalance import EXPECTED
            assets = rebalance.get('assets', [])
            require(len(assets) == len(EXPECTED) and {a.get('symbol') for a in assets} == set(EXPECTED), 'rebalance assets incomplete')
            require(all(positive(a.get('net_usdt')) and positive(a.get('target_weight_pct')) for a in assets), 'invalid rebalance value')
            require(abs(sum(a.get('target_weight_pct', 0) for a in assets) - 100) < 0.001, 'rebalance weights do not sum to 100')
            require(positive(rebalance.get('investment_usdt')), 'invalid investment')
            require(abs(sum(a.get('net_usdt', 0) for a in assets) - rebalance.get('net_value_usdt', 0)) < 0.02, 'rebalance total mismatch')
    except (ValueError, TypeError, KeyError, OSError) as exc:
        errors.append(f'invalid or missing data: {type(exc).__name__}')
    return {'ok': not errors, 'date': today, 'kind': kind, 'errors': errors}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('grok', 'pionex', 'all'), default='all')
    args = parser.parse_args()
    results = [inspect(k) for k in (('grok', 'pionex') if args.kind == 'all' else (args.kind,))]
    print(json.dumps({'ok': all(r['ok'] for r in results), 'checks': results}, ensure_ascii=False))
    raise SystemExit(0 if all(r['ok'] for r in results) else 1)
