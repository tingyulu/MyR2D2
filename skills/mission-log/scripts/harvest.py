#!/usr/bin/env python3
# harvest.py — mission-log 零 token 收割器(原型 v0)
# 從 ~/.claude/projects/*/*.jsonl 抽出指定日期(當地時區)的活動骨架。
# 純標準庫、不呼叫任何模型;可整檔經 ssh 餵給遠端 python3(單檔自包含)。
# 用法: python3 harvest.py [--date YYYY-MM-DD] [--dir DIR] [--format md|jsonl]
# 語意約定:
# - tokens = 新增 tokens(input+output+cache_creation),不含 cache 讀取——與生態通用口徑一致。
# - 查詢日 = 本機時區的日曆日;session 在該日 00:00-24:00 間有任何活動即計入。
# - 🔴 一則 API 回應只算一次:transcript 把同一則回應拆成多行(每個 content block 一行),每行都帶同一份
#   usage,而 output_tokens 是串流累計快照——逐行相加會把 turns 與 tokens 灌水約 2 倍(實測每天 1.6~7 倍不等,
#   不能用固定倍率還原)。做法:按 message.id 併成一則(沒 id 的行用 requestId 併回唯一對到的那則;兩個都沒有
#   才逐行各算一則),四欄各取最大;turns = 回應數(含 0 token 的 <synthetic>);跨午夜的回應歸它第一行那天。
# - 🔴 沒見過的形狀不硬加、要回報:同 id 出現兩組都非零且不相等的 input/cache、同 id 對到兩個 requestId、
#   同一個 requestId 對到兩個以上(非 synthetic 的)id ⇒ 該則標矛盾:turns 照算、tokens 不計入、
#   session 記 usage_conflict、stderr 警告。取最大會拼出從沒存在過的用量,寧可擋。
import json, sys, os, glob, argparse, datetime

USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
SYNTHETIC = '<synthetic>'

def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone()
    except Exception:
        return None

def user_text(msg):
    """抽出「真人打的字」:排除 tool_result/系統注入(<開頭)/指令包裝。"""
    c = msg.get('content')
    texts = []
    if isinstance(c, str):
        texts = [c]
    elif isinstance(c, list):
        texts = [b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text']
    for t in texts:
        t = t.strip()
        if t and not t.startswith('<') and not t.startswith('Caveat:'):
            return t
    return None

def usage_vals(u):
    """usage 四欄 → 非負整數 list;沒有這個鍵=0。型別不對(字串/bool/負數)回 None,呼叫端計壞行、不猜不靜默當 0。"""
    out = []
    for k in USAGE_KEYS:
        x = u.get(k)
        if x is None:
            x = 0
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            return None
        out.append(x)
    return out

def merge_response(resp, key, vals, rq):
    """把一行的 usage 併進 resp[key](同一則回應)。四欄取最大;input/cache 簽名或 requestId 對不上就標矛盾。"""
    sig = (vals[0], vals[2], vals[3])
    sig = sig if any(sig) else None
    e = resp.get(key)
    if e is None:
        resp[key] = {'v': list(vals), 'sig': sig, 'req': rq, 'bad': None}
        return
    for i in range(4):
        if vals[i] > e['v'][i]:
            e['v'][i] = vals[i]
    if sig is not None:
        if e['sig'] is None:
            e['sig'] = sig
        elif sig != e['sig'] and e['bad'] is None:
            e['bad'] = '同 id 用量矛盾'
    if rq:
        if e['req'] is None:
            e['req'] = rq
        elif rq != e['req'] and e['bad'] is None:
            e['bad'] = '同 id 對到兩個 requestId'

def resolve_aliases(resp, req2ids):
    """沒有 message.id 的行(key=('req', q)):q 唯一對到一個 id ⇒ 併進那則;對到多個 ⇒ 矛盾。
    同一個 requestId 對到兩個以上非 synthetic 的 id ⇒ 那幾則都標矛盾。"""
    for key in [k for k in resp if k[0] == 'req']:
        ids = req2ids.get(key[1], set())
        if len(ids) == 1:
            e = resp.pop(key)
            tgt = ('id', next(iter(ids)))
            merge_response(resp, tgt, e['v'], e['req'])
            if e['bad'] and resp[tgt]['bad'] is None:
                resp[tgt]['bad'] = e['bad']
        elif len(ids) > 1 and resp[key]['bad'] is None:
            resp[key]['bad'] = 'requestId 對到多個 id'
    for q, ids in req2ids.items():
        if len(ids) > 1:
            for mid in ids:
                e = resp.get(('id', mid))
                if e is not None and e['bad'] is None:
                    e['bad'] = 'requestId 對到多個 id'

def account_assistant(s, obj, m, mid, rq, lineno, resp, req2ids):
    """一行 assistant 訊息的 usage/模型/工具記進 session。回 1=usage 欄壞行(已略過),0=正常。"""
    bad = 0
    u = m.get('usage')
    if isinstance(u, dict):
        vals = usage_vals(u)
        if vals is None:
            bad = 1
        else:
            key = ('id', mid) if mid else (('req', rq) if rq else ('line', lineno))
            if mid and rq and m.get('model') != SYNTHETIC:
                req2ids.setdefault(rq, set()).add(mid)
            merge_response(resp, key, vals, rq)
    if m.get('model') and m['model'] != SYNTHETIC:
        s['models'].add(m['model'])
    c = m.get('content')
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get('type') == 'tool_use':
                n = b.get('name', '?')
                s['tools'][n] = s['tools'].get(n, 0) + 1
    return bad

def harvest(projects_dir, day):
    day_start = datetime.datetime.combine(day, datetime.time.min).astimezone()
    day_end = day_start + datetime.timedelta(days=1)
    sessions = {}
    bad_ts = bad_usage = conflicts = 0
    # mtime 粗篩:整檔最後修改早於當天開始的不可能含當天資料
    for tx in sorted(glob.glob(os.path.join(projects_dir, '*', '*.jsonl'))):
        try:
            if datetime.datetime.fromtimestamp(os.path.getmtime(tx)).astimezone() < day_start:
                continue
        except OSError:
            continue
        proj = os.path.basename(os.path.dirname(tx))
        sid = os.path.basename(tx)[:8]
        key = (proj, sid)
        s = None
        pre_ids = set()        # 當天開始前就出現過的回應 id:跨午夜的回應歸前一天,它今天的行整行不算
        resp, req2ids = {}, {}  # 本檔當天的「一則回應」表 / requestId → 非 synthetic 的 message.id
        with open(tx, encoding='utf-8', errors='replace') as fh:
            for lineno, line in enumerate(fh, 1):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                raw = obj.get('timestamp')
                ts = parse_ts(raw) if raw else None
                if raw and ts is None:
                    bad_ts += 1  # 有時間戳但解析不了:計數;解析失敗不可觸發下面的提早停止
                    continue
                if not ts:
                    continue
                m = obj.get('message')
                if not isinstance(m, dict):
                    m = None
                has_usage = m is not None and isinstance(m.get('usage'), dict)
                mid = m.get('id') if has_usage else None
                mid = mid if isinstance(mid, str) and mid else None
                rq = obj.get('requestId')
                rq = rq if isinstance(rq, str) and rq else None
                if ts < day_start:
                    if mid:
                        pre_ids.add(mid)
                    continue
                if ts >= day_end:
                    # 跨午夜:同一則回應的後續行會落在當天終點之後——只吸收當天已開頭的那則,遇到新回應就停
                    # (transcript 是 append-only 時序檔,過了查詢日終點即可停,跨日長檔有感)
                    if has_usage and s is not None and mid and ('id', mid) in resp:
                        bad_usage += account_assistant(s, obj, m, mid, rq, lineno, resp, req2ids)
                        continue
                    if has_usage:
                        break
                    continue
                if mid and mid in pre_ids:
                    continue  # 這行屬於前一天開始的回應,整行(含工具)歸那天
                s = sessions.setdefault(key, {
                    'project': proj.split('-')[-1] or proj, 'session': sid,
                    'first': ts, 'last': ts, 'turns': 0, 'tokens': 0, 'usage_conflict': 0,
                    'tools': {}, 'models': set(), 'prompts': [], 'branch': None, '_cwd': None})
                s['first'] = min(s['first'], ts); s['last'] = max(s['last'], ts)
                if obj.get('cwd') and not s['_cwd']:
                    s['_cwd'] = obj['cwd']
                    s['project'] = os.path.basename(obj['cwd'].rstrip('/')) or s['project']
                if obj.get('gitBranch') and not s['branch']:
                    s['branch'] = obj['gitBranch']
                if m is None:
                    continue
                if obj.get('type') == 'user' and not obj.get('isMeta'):
                    t = user_text(m)
                    if t:
                        s['prompts'].append(t[:70])
                bad_usage += account_assistant(s, obj, m, mid, rq, lineno, resp, req2ids)
        if s is not None:
            resolve_aliases(resp, req2ids)
            for e in resp.values():
                s['turns'] += 1
                if e['bad']:
                    s['usage_conflict'] += 1
                else:
                    s['tokens'] += e['v'][0] + e['v'][1] + e['v'][2]
            conflicts += s['usage_conflict']
    return sorted(sessions.values(), key=lambda s: s['first']), bad_ts, bad_usage, conflicts

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None)
    ap.add_argument('--dir', default=os.path.expanduser('~/.claude/projects'))
    ap.add_argument('--format', choices=['md', 'jsonl'], default='md')
    a = ap.parse_args()
    day = datetime.date.fromisoformat(a.date) if a.date else (datetime.date.today() - datetime.timedelta(days=1))
    rows, bad_ts, bad_usage, conflicts = harvest(a.dir, day)
    if bad_ts:
        print(f"⚠️ {bad_ts} 行時間戳無法解析已略過", file=sys.stderr)
    if bad_usage:
        print(f"⚠️ {bad_usage} 行 usage 欄型別不對已略過(不猜、不當 0)", file=sys.stderr)
    if conflicts:
        print(f"⚠️ {conflicts} 則回應用量矛盾(同 id 兩組不同用量或對到兩個 requestId):turns 照算、tokens 未計入,見各列 usage_conflict", file=sys.stderr)
    host = os.uname().nodename.split('.')[0]
    if a.format == 'jsonl':
        for s in rows:
            out = dict(s, first=s['first'].strftime('%H:%M'), last=s['last'].strftime('%H:%M'),
                       models=sorted(s['models']), host=host,
                       tools=dict(sorted(s['tools'].items(), key=lambda x: -x[1])[:6]),
                       prompts=s['prompts'][:5] + (['…+%d' % (len(s['prompts']) - 5)] if len(s['prompts']) > 5 else []))
            print(json.dumps(out, ensure_ascii=False, default=str))
        return
    print(f"## {day} @ {host} — {len(rows)} 個活躍 session (tok=新增 in+out+cache_creation, 不含 cache 讀取; turns=回應數, 同一則回應拆成的多行只算一次)")
    for s in rows:
        tools = ' '.join(f"{k}×{v}" for k, v in sorted(s['tools'].items(), key=lambda x: -x[1])[:4])
        models = ','.join(m.split('-')[1] for m in sorted(s['models']))
        warn = f" ⚠️用量矛盾×{s['usage_conflict']}" if s['usage_conflict'] else ''
        print(f"\n### {s['first'].strftime('%H:%M')}–{s['last'].strftime('%H:%M')}  {s['project']}"
              f"{'@' + s['branch'] if s['branch'] else ''}  ({s['turns']} turns, {s['tokens']:,} tok{warn}, {models})")
        print(f"  工具: {tools or '（無）'}")
        for p in s['prompts'][:5]:
            print(f"  › {p}")
        if len(s['prompts']) > 5:
            print(f"  › …共 {len(s['prompts'])} 句")

if __name__ == '__main__':
    main()
