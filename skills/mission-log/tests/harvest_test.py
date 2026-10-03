#!/usr/bin/env python3
# harvest_test.py — mission-log 收割器的合成 fixture 測試(純標準庫,零依賴)
# 跑法: python3 skills/mission-log/tests/harvest_test.py
# 全部走子行程實跑(測的是真實 CLI 行為),TZ 固定 Asia/Taipei 讓日界線判斷可重現;
# LC_ALL=C 那條驗的是「ssh 到 C locale 機器」情境下中文不得變 ? 或替換字元。
import json, os, subprocess, sys, tempfile, unittest

HARVEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'harvest.py')

# Taipei(+08:00) 的 2026-01-05 00:00 = UTC 2026-01-04T16:00:00Z
DAY = '2026-01-05'


def user_line(ts, text, cwd=None):
    d = {'timestamp': ts, 'type': 'user', 'message': {'role': 'user', 'content': text}}
    if cwd:
        d['cwd'] = cwd
    return d


def asst_line(ts, usage=None, tools=None, model='claude-test-1', mid=None, req=None):
    msg = {'role': 'assistant', 'model': model,
           'usage': usage or {'input_tokens': 10, 'output_tokens': 5,
                              'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 100}}
    if tools is not None:
        msg['content'] = tools
    if mid:
        msg['id'] = mid          # 同一則 API 回應拆成的多行共用同一個 message.id(真 transcript 的形狀)
    d = {'timestamp': ts, 'type': 'assistant', 'message': msg}
    if req:
        d['requestId'] = req
    return d


def usage(out, inp=10, cc=3, cr=100):
    return {'input_tokens': inp, 'output_tokens': out, 'cache_creation_input_tokens': cc, 'cache_read_input_tokens': cr}


class HarvestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write_session(self, proj, name, lines):
        d = os.path.join(self.root, proj)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + '.jsonl'), 'w', encoding='utf-8') as fh:
            for ln in lines:
                fh.write(ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False))
                fh.write('\n')

    def run_harvest(self, date=DAY, fmt='jsonl', env_extra=None):
        env = dict(os.environ, TZ='Asia/Taipei')
        env.update(env_extra or {})
        return subprocess.run([sys.executable, HARVEST, '--date', date, '--dir', self.root, '--format', fmt],
                              capture_output=True, env=env)

    def rows(self, p):
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
        return [json.loads(l) for l in p.stdout.decode('utf-8').splitlines() if l.strip()]

    def test_date_filter(self):
        self.write_session('proj-a', 'aaaa1111', [
            user_line('2026-01-05T04:00:00Z', '把報告寫完'),
            asst_line('2026-01-05T04:01:00Z'),
        ])
        self.write_session('proj-b', 'bbbb2222', [
            user_line('2026-01-08T04:00:00Z', '別的日子'),
            asst_line('2026-01-08T04:01:00Z'),
        ])
        rows = self.rows(self.run_harvest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session'], 'aaaa1111')
        self.assertEqual(rows[0]['prompts'], ['把報告寫完'])

    def test_cross_day_attribution(self):
        # Taipei 23:50 與翌日 00:10;同一個 session 兩天都該計入,各只算落在該日的活動
        self.write_session('proj-x', 'cccc3333', [
            asst_line('2026-01-05T15:50:00Z'),   # Taipei 01-05 23:50
            asst_line('2026-01-05T16:10:00Z'),   # Taipei 01-06 00:10
        ])
        d5 = self.rows(self.run_harvest('2026-01-05'))
        self.assertEqual(len(d5), 1)
        self.assertEqual(d5[0]['turns'], 1)
        self.assertEqual(d5[0]['last'], '23:50')
        d6 = self.rows(self.run_harvest('2026-01-06'))
        self.assertEqual(len(d6), 1)
        self.assertEqual(d6[0]['first'], '00:10')

    def test_bad_json_line(self):
        self.write_session('proj-a', 'dddd4444', [
            user_line('2026-01-05T04:00:00Z', '前面正常'),
            '{this is not json at all',
            asst_line('2026-01-05T04:02:00Z'),
        ])
        rows = self.rows(self.run_harvest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['turns'], 1)

    def test_missing_tool_name(self):
        self.write_session('proj-a', 'eeee5555', [
            asst_line('2026-01-05T04:00:00Z',
                      tools=[{'type': 'tool_use', 'name': 'Bash'}, {'type': 'tool_use'}]),
        ])
        rows = self.rows(self.run_harvest())
        self.assertEqual(rows[0]['tools'].get('Bash'), 1)
        self.assertEqual(rows[0]['tools'].get('?'), 1)

    def test_cjk_cwd_in_md(self):
        self.write_session('encoded-dir-name', 'ffff6666', [
            user_line('2026-01-05T04:00:00Z', '中文原話要完整保留', cwd='/tmp/測試中文專案'),
            asst_line('2026-01-05T04:01:00Z'),
        ])
        p = self.run_harvest(fmt='md')
        out = p.stdout.decode('utf-8')
        self.assertIn('測試中文專案', out)
        self.assertIn('中文原話要完整保留', out)

    def test_token_definition(self):
        # tokens = in+out+cache_creation(10+5+3=18),cache_read(100)不計
        self.write_session('proj-a', 'gggg7777', [asst_line('2026-01-05T04:00:00Z')])
        rows = self.rows(self.run_harvest())
        self.assertEqual(rows[0]['tokens'], 18)

    def test_lc_all_c_subprocess(self):
        self.write_session('encoded-dir-name', 'hhhh8888', [
            user_line('2026-01-05T04:00:00Z', '中文原話要完整保留', cwd='/tmp/測試中文專案'),
            asst_line('2026-01-05T04:01:00Z'),
        ])
        p = self.run_harvest(fmt='md', env_extra={'LC_ALL': 'C', 'LANG': 'C'})
        self.assertEqual(p.returncode, 0, p.stderr.decode('utf-8', 'replace'))
        out = p.stdout.decode('utf-8')
        self.assertIn('測試中文專案', out)
        self.assertIn('中文原話要完整保留', out)
        self.assertNotIn('�', out)

    def test_bad_timestamp_warning(self):
        self.write_session('proj-a', 'iiii9999', [
            {'timestamp': 'not-a-date', 'type': 'user', 'message': {'role': 'user', 'content': '壞戳'}},
            user_line('2026-01-05T04:00:00Z', '好戳'),
            asst_line('2026-01-05T04:01:00Z'),
        ])
        p = self.run_harvest()
        rows = self.rows(p)
        self.assertEqual(len(rows), 1)
        err = p.stderr.decode('utf-8')
        self.assertIn('1 行時間戳無法解析已略過', err)

    # ---- 一則回應只算一次(transcript 每個 content block 一行、同一份 usage 重抄;逐行加會灌水約 2 倍)
    def test_one_response_counted_once(self):
        # 同 id 三行,output 是累計快照 5→20→60 ⇒ 只算一則、四欄取最大;另一則 msg_B 照算
        self.write_session('proj-a', 'jjjj0001', [
            asst_line('2026-01-05T04:00:00Z', usage(5), mid='msg_A', req='req_1'),
            asst_line('2026-01-05T04:00:01Z', usage(20), mid='msg_A', req='req_1'),
            asst_line('2026-01-05T04:00:02Z', usage(60), mid='msg_A', req='req_1'),
            asst_line('2026-01-05T04:01:00Z', usage(7), mid='msg_B', req='req_2'),
        ])
        # 負對照:同樣四行但 id 各不同 ⇒ 四則各算,證明去重只認同一個 id、不是「連續行就併」
        self.write_session('proj-b', 'jjjj0002', [
            asst_line('2026-01-05T04:00:0%dZ' % i, usage(5), mid='msg_%d' % i) for i in range(4)])
        rows = {r['session']: r for r in self.rows(self.run_harvest())}
        self.assertEqual(rows['jjjj0001']['turns'], 2)
        self.assertEqual(rows['jjjj0001']['tokens'], (10 + 60 + 3) + (10 + 7 + 3))
        self.assertEqual(rows['jjjj0001']['usage_conflict'], 0)
        self.assertEqual(rows['jjjj0002']['turns'], 4)
        self.assertEqual(rows['jjjj0002']['tokens'], 4 * (10 + 5 + 3))

    def test_requestid_alias_and_synthetic(self):
        # 沒有 message.id 的行:requestId 唯一對到 msg_A ⇒ 併進去;<synthetic> 借同一個 requestId 但有自己的 id
        # ⇒ 自己算一則、0 token、不算矛盾;id 與 requestId 都沒有 ⇒ 自己算一則
        self.write_session('proj-a', 'kkkk0001', [
            asst_line('2026-01-05T04:00:00Z', usage(5), mid='msg_A', req='req_1'),
            asst_line('2026-01-05T04:00:01Z', usage(30), req='req_1'),
            asst_line('2026-01-05T04:00:02Z', usage(0, 0, 0, 0), mid='msg_S', req='req_1', model='<synthetic>'),
            asst_line('2026-01-05T04:00:03Z', usage(9)),
        ])
        p = self.run_harvest()
        rows = self.rows(p)
        self.assertEqual(rows[0]['turns'], 3)
        self.assertEqual(rows[0]['tokens'], (10 + 30 + 3) + 0 + (10 + 9 + 3))
        self.assertEqual(rows[0]['usage_conflict'], 0)
        self.assertNotIn('矛盾', p.stderr.decode('utf-8'))

    def test_usage_conflict_reported_not_summed(self):
        # 同 id 兩組都非零且不相等的 input ⇒ 矛盾:turns 照算一則、tokens 不硬加(只剩 msg_B)、usage_conflict=1、stderr 警告
        self.write_session('proj-a', 'llll0001', [
            asst_line('2026-01-05T04:00:00Z', usage(5, inp=10), mid='msg_A'),
            asst_line('2026-01-05T04:00:01Z', usage(30, inp=20), mid='msg_A'),
            asst_line('2026-01-05T04:01:00Z', usage(4), mid='msg_B'),
        ])
        # 同 id 對到兩個 requestId 也是矛盾
        self.write_session('proj-b', 'llll0002', [
            asst_line('2026-01-05T04:00:00Z', usage(5), mid='msg_C', req='req_1'),
            asst_line('2026-01-05T04:00:01Z', usage(5), mid='msg_C', req='req_2'),
        ])
        p = self.run_harvest()
        rows = {r['session']: r for r in self.rows(p)}
        self.assertEqual(rows['llll0001']['turns'], 2)
        self.assertEqual(rows['llll0001']['usage_conflict'], 1)
        self.assertEqual(rows['llll0001']['tokens'], 10 + 4 + 3)
        self.assertEqual(rows['llll0002']['turns'], 1)
        self.assertEqual(rows['llll0002']['usage_conflict'], 1)
        self.assertEqual(rows['llll0002']['tokens'], 0)
        self.assertIn('2 則回應用量矛盾', p.stderr.decode('utf-8'))
        md = self.run_harvest(fmt='md').stdout.decode('utf-8')
        self.assertIn('用量矛盾×1', md)

    def test_midnight_straddle_belongs_to_first_line_day(self):
        # 回應第一行 Taipei 01-05 23:59:58、後續行 01-06 00:00:02 ⇒ 整則歸 01-05(output 取含跨日行的最大、工具也算進去);
        # 01-06 不再算這則、也不因它多出一個 session;01-06 真正的活動(msg_B)照常
        self.write_session('proj-a', 'mmmm0001', [
            asst_line('2026-01-05T15:59:58Z', usage(5), mid='msg_A', tools=[{'type': 'tool_use', 'name': 'Bash'}]),
            asst_line('2026-01-05T16:00:02Z', usage(40), mid='msg_A', tools=[{'type': 'tool_use', 'name': 'Read'}]),
            asst_line('2026-01-05T16:05:00Z', usage(2), mid='msg_B'),
        ])
        d5 = self.rows(self.run_harvest('2026-01-05'))
        self.assertEqual(len(d5), 1)
        self.assertEqual(d5[0]['turns'], 1)
        self.assertEqual(d5[0]['tokens'], 10 + 40 + 3)
        self.assertEqual(d5[0]['tools'], {'Bash': 1, 'Read': 1})
        self.assertEqual(d5[0]['last'], '23:59')
        d6 = self.rows(self.run_harvest('2026-01-06'))
        self.assertEqual(len(d6), 1)
        self.assertEqual(d6[0]['turns'], 1)
        self.assertEqual(d6[0]['tokens'], 10 + 2 + 3)
        self.assertEqual(d6[0]['first'], '00:05')
        self.assertEqual(d6[0]['tools'], {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
