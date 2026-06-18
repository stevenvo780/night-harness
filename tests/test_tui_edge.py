#!/usr/bin/env python3
"""Edge-case tests for tui.py helpers — focus on malformed inputs, type
coercion corner cases, and failure modes that the curses TUI cannot easily
exercise on its own.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tui
except ImportError as e:
    if 'curses' in str(e):
        tui = None  # type: ignore[assignment]
    else:
        raise


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestReadStatusEdgeCases(unittest.TestCase):
    """read_status: unusual but valid JSON shapes and failure modes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'status.json')

    def _write_raw(self, text):
        with open(self.path, 'w') as f:
            f.write(text)

    def test_status_with_extra_fields_preserved(self):
        # All original keys must survive schema_version check.
        self._write_raw(json.dumps({
            'schema_version': 2,
            'pid': 1,
            'uptime_sec': 100,
            'funnel': {'bugs_confirmed': 0},
            'roles': [],
            'nodes': [],
            'threads': [],
        }))
        result = tui.read_status(self.path)
        self.assertEqual(result['pid'], 1)
        self.assertEqual(result['uptime_sec'], 100)
        self.assertIn('funnel', result)
        self.assertNotIn('_schema_warning', result)

    def test_status_with_string_schema_version(self):
        # A malformed schema_version (string) != SCHEMA_VERSION -> warning.
        self._write_raw(json.dumps({'schema_version': '2', 'pid': 1}))
        result = tui.read_status(self.path)
        self.assertIn('_schema_warning', result)

    def test_status_with_null_schema_version(self):
        # JSON null -> 'None != 2' -> warning.
        self._write_raw(json.dumps({'schema_version': None, 'pid': 1}))
        result = tui.read_status(self.path)
        self.assertIn('_schema_warning', result)

    def test_status_v2_with_funnel_partial(self):
        # A v2 status with a partial funnel must still load.
        self._write_raw(json.dumps({
            'schema_version': 2,
            'funnel': {'bugs_confirmed': 5},  # most fields missing
        }))
        result = tui.read_status(self.path)
        self.assertNotIn('_schema_warning', result)
        self.assertEqual(result['funnel']['bugs_confirmed'], 5)

    def test_status_v2_with_empty_dict(self):
        # Schema v2 with no payload is fine — no warning.
        self._write_raw(json.dumps({'schema_version': 2}))
        result = tui.read_status(self.path)
        self.assertEqual(result, {'schema_version': 2})

    def test_status_truncated_json(self):
        # Half a JSON object -> parse error -> empty dict.
        self._write_raw('{"schema_version": 2, "pid":')
        result = tui.read_status(self.path)
        self.assertEqual(result, {})

    def test_status_path_is_directory_returns_empty(self):
        # A path that is a directory can't be read as a file -> empty dict.
        result = tui.read_status(self.tmp.name)
        self.assertEqual(result, {})

    def test_status_v2_unicode_content(self):
        # Non-ASCII payload should be preserved.
        self._write_raw(json.dumps({
            'schema_version': 2,
            'memory': {'note': 'áéíóú 中文'},
        }, ensure_ascii=False))
        result = tui.read_status(self.path)
        self.assertEqual(result['memory']['note'], 'áéíóú 中文')

    def test_schema_warning_message_format(self):
        # The warning string should include both the actual and expected
        # version so debugging is easy.
        self._write_raw(json.dumps({'schema_version': 99}))
        result = tui.read_status(self.path)
        msg = result['_schema_warning']
        self.assertIn('99', msg)
        self.assertIn(str(tui.SCHEMA_VERSION), msg)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeConversionsEdgeCases(unittest.TestCase):
    """_safe_int / _safe_float: pathological inputs that must NOT crash."""

    def test_safe_int_empty_string(self):
        # int('') raises ValueError -> default 0.
        self.assertEqual(tui._safe_int(''), 0)

    def test_safe_int_whitespace_string(self):
        # int(' ') raises ValueError -> default.
        self.assertEqual(tui._safe_int('   '), 0)

    def test_safe_int_float_string(self):
        # int('3.14') raises ValueError -> default.
        self.assertEqual(tui._safe_int('3.14'), 0)

    def test_safe_int_list_returns_default(self):
        self.assertEqual(tui._safe_int([1, 2, 3]), 0)

    def test_safe_int_object_returns_default(self):
        class Foo: pass
        self.assertEqual(tui._safe_int(Foo()), 0)

    def test_safe_int_negative_default(self):
        self.assertEqual(tui._safe_int(None, -1), -1)

    def test_safe_float_nan_returns_nan(self):
        # float('nan') is valid; we don't try to reject it.
        result = tui._safe_float('nan')
        import math
        self.assertTrue(math.isnan(result))

    def test_safe_float_infinity(self):
        # float('inf') is also valid.
        result = tui._safe_float('inf')
        import math
        self.assertTrue(math.isinf(result))

    def test_safe_float_bool_is_int(self):
        # bool is a subclass of int — accept it.
        self.assertEqual(tui._safe_float(True), 1.0)
        self.assertEqual(tui._safe_float(False), 0.0)

    def test_safe_int_bool_is_int(self):
        self.assertEqual(tui._safe_int(True), 1)
        self.assertEqual(tui._safe_int(False), 0)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeGetFunnelEdgeCases(unittest.TestCase):
    """_safe_get_funnel: every shape of bad input."""

    def test_funnel_none_falls_back_to_stats(self):
        status = {'funnel': None, 'stats': {'bugs_confirmed': 4}}
        f = tui._safe_get_funnel(status)
        self.assertEqual(f['bugs_confirmed'], 4)

    def test_funnel_empty_dict_returned_as_is(self):
        # When 'funnel' is a dict (even empty), it's returned as-is — no
        # default-merge. The defaults are only added when 'funnel' is
        # missing or not a dict.
        f = tui._safe_get_funnel({'funnel': {}})
        self.assertEqual(f, {})

    def test_funnel_missing_returns_full_defaults(self):
        # When 'funnel' is absent, we get a dict with all 12 default keys.
        f = tui._safe_get_funnel({})
        self.assertEqual(len(f), 12)
        self.assertEqual(f['bugs_confirmed'], 0)
        self.assertEqual(f['fixes_succeeded'], 0)

    def test_stats_none_returns_zeros(self):
        f = tui._safe_get_funnel({'funnel': None, 'stats': None})
        # stats None -> empty defaults -> all zeros.
        self.assertEqual(f['rounds'], 0)

    def test_v1_stats_with_garbage_values(self):
        # Garbage values in v1 stats must NOT crash _safe_int.
        status = {
            'schema_version': 1,
            'stats': {
                'rounds': 'not a number',
                'bugs_confirmed': None,
                'fixes_succeeded': [1, 2, 3],  # list, not int
            },
        }
        f = tui._safe_get_funnel(status)
        # Bad values collapse to 0; valid ones pass through.
        self.assertEqual(f['rounds'], 0)
        self.assertEqual(f['bugs_confirmed'], 0)
        self.assertEqual(f['fixes_succeeded'], 0)

    def test_v2_funnel_overrides_stats(self):
        # If both are present (mixed bag), v2 funnel wins.
        status = {
            'schema_version': 2,
            'funnel': {'bugs_confirmed': 7},
            'stats': {'bugs_confirmed': 999},  # should be ignored
        }
        f = tui._safe_get_funnel(status)
        self.assertEqual(f['bugs_confirmed'], 7)

    def test_v1_legacy_field_name_aliases(self):
        # v1 used 'suspects_refuted_by_filter' instead of 'suspects_refuted'.
        status = {
            'schema_version': 1,
            'stats': {
                'suspects_refuted_by_filter': 12,
                'suspects_filtered_as_known_fp': 3,
            },
        }
        f = tui._safe_get_funnel(status)
        # The aliases are mapped to the v2 names.
        self.assertEqual(f['suspects_refuted'], 12)
        self.assertEqual(f['suspects_known_fp'], 3)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestModelConfigEdgeCases(unittest.TestCase):
    """_save_model_config: failure modes on write."""

    def test_save_to_unwritable_path_is_silent(self):
        # A path under a non-existent directory -> OSError swallowed.
        bad_path = '/nonexistent/dir/that/does/not/exist/models.json'
        # Must not raise.
        tui._save_model_config(bad_path, {'tier0_local': ['x']})

    def test_load_after_failed_save_returns_defaults(self):
        bad_path = '/nonexistent/dir/models.json'
        config = tui._load_model_config(bad_path)
        self.assertIn('tier0_local', config)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestLogBufferEdgeCases(unittest.TestCase):
    """LogBuffer: rotation, max-lines-at-construction, weird timestamps."""

    def test_zero_max_lines(self):
        # max_lines=0 -> deque never holds anything, but should not crash.
        lb = tui.LogBuffer(max_lines=0)
        lb.append_msg('should be dropped')
        self.assertEqual(len(lb.lines), 0)

    def test_default_max_lines_is_config_constant(self):
        # Construction without args uses LOG_LINES constant.
        lb = tui.LogBuffer()
        self.assertEqual(lb.max, tui.LOG_LINES)

    def test_scroll_on_empty_buffer(self):
        # Scrolling an empty buffer must not raise or go negative.
        lb = tui.LogBuffer(max_lines=10)
        lb.scroll_up(5)
        self.assertEqual(lb._cursor, 0)
        lb.scroll_down(5)
        self.assertEqual(lb._cursor, 0)

    def test_update_handles_lines_without_timestamp(self):
        # Lines that don't match [HH:MM:SS] should still be appended,
        # just with a "now" timestamp.
        lb = tui.LogBuffer(max_lines=10)
        path = os.path.join(tempfile.mkdtemp(), 'nolines.log')
        with open(path, 'w') as f:
            f.write('plain line with no timestamp\n')
        lb.update(path)
        self.assertEqual(len(lb.lines), 1)
        self.assertIn('plain line', lb.lines[0][1])

    def test_update_with_garbled_timestamp_uses_now(self):
        # [99:99:99] is not a real time -> mktime raises -> falls back to now.
        lb = tui.LogBuffer(max_lines=10)
        path = os.path.join(tempfile.mkdtemp(), 'badts.log')
        with open(path, 'w') as f:
            f.write('[99:99:99] impossible time\n')
        lb.update(path)
        self.assertEqual(len(lb.lines), 1)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestReadLogCallsEdgeCases(unittest.TestCase):
    """read_log_calls: log lines that partially match the regex."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'h.log')

    def test_call_line_without_model_id_is_skipped(self):
        # Regex requires a non-space token after [call]; empty -> no match.
        ts = time.strftime('%H:%M:%S')
        with open(self.path, 'w') as f:
            f.write(f'[{ts}] [call] \n')  # no model id
        counts = tui.read_log_calls(self.path, window_sec=120)
        self.assertEqual(counts, Counter())

    def test_call_line_with_extra_spaces_in_model(self):
        # Model id with a space should not match the \S+ group.
        ts = time.strftime('%H:%M:%S')
        with open(self.path, 'w') as f:
            f.write(f'[{ts}] [call] ollama:has space some args\n')
        counts = tui.read_log_calls(self.path, window_sec=120)
        # "ollama:has" matches \S+, the rest is captured by ' ' literal.
        self.assertEqual(counts.get('ollama:has', 0), 1)

    def test_empty_log_file(self):
        # An empty log file yields an empty Counter.
        Path(self.path).touch()
        counts = tui.read_log_calls(self.path, window_sec=60)
        self.assertEqual(counts, Counter())

    def test_log_file_with_only_old_timestamps(self):
        # Use a timestamp that's certainly in the past relative to "now".
        # We pick 00:00:01 — this is risky near midnight, but the test
        # also asserts the function is well-behaved when nothing matches.
        with open(self.path, 'w') as f:
            f.write('[00:00:01] [call] ollama:stale-model running\n')
        counts = tui.read_log_calls(self.path, window_sec=1)
        # Whether or not 00:00:01 is in the window, we must not crash.
        self.assertIsInstance(counts, Counter)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestCheckAliveEdgeCases(unittest.TestCase):
    """check_alive: edge-case pids."""

    def test_pid_zero_does_not_crash(self):
        # On Linux, os.kill(0, 0) is "send to my process group" and
        # always succeeds — so check_alive(0) returns True. The contract
        # is just "no exception is raised".
        result = tui.check_alive(0)
        self.assertIsInstance(result, bool)

    def test_negative_pid_does_not_crash(self):
        # On Linux, os.kill(-1, 0) is "send to every process I can
        # signal" and typically succeeds. We just verify no exception.
        result = tui.check_alive(-1)
        self.assertIsInstance(result, bool)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestShortModelEdgeCases(unittest.TestCase):
    """_short_model: extreme inputs."""

    def test_empty_string(self):
        self.assertEqual(tui._short_model(''), '')

    def test_only_colon(self):
        # ':' -> split(':', 1) -> ['', ''] -> second element ''.
        self.assertEqual(tui._short_model(':'), '')

    def test_only_slash(self):
        self.assertEqual(tui._short_model('/'), '')

    def test_trailing_separator(self):
        # 'foo:' -> split(':', 1) -> ['foo', ''] -> ''.
        self.assertEqual(tui._short_model('foo:'), '')


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestModelFamilyShortEdgeCases(unittest.TestCase):
    """model_family_short: unknown families and edge cases."""

    def test_empty_string_returns_empty(self):
        # Empty input -> split returns [''] -> ''.
        self.assertEqual(tui.model_family_short(''), '')

    def test_partial_match_does_not_match(self):
        # 'ollama:something-else' is NOT in the known set.
        result = tui.model_family_short('ollama:something-else')
        # Falls back to last segment after ':'.
        self.assertEqual(result, 'something-else')

    def test_case_sensitive(self):
        # The matching is case-sensitive: 'Ollama:qwen3.5:9b' does NOT match.
        result = tui.model_family_short('Ollama:qwen3.5:9b')
        # Falls through to the truncation branch.
        self.assertEqual(result, '9b')


if __name__ == '__main__':
    unittest.main(verbosity=2)
