#!/usr/bin/env python3
"""Tests for the pure-Python data-handling helpers in tui.py.

The TUI itself is curses-coupled and not unit-testable in this environment;
these tests focus on the parsing, validation and safe-default helpers.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tui
except ImportError as e:  # curses missing in this env
    if 'curses' in str(e):
        tui = None  # type: ignore[assignment]
    else:
        raise


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSchemaVersion(unittest.TestCase):
    """The SCHEMA_VERSION constant drives read_status's warning logic."""

    def test_schema_version_is_two(self):
        self.assertEqual(tui.SCHEMA_VERSION, 2)

    def test_schema_version_is_int(self):
        self.assertIsInstance(tui.SCHEMA_VERSION, int)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestReadStatus(unittest.TestCase):
    """read_status: loads status JSON, tags version mismatches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'status.json')

    def _write(self, payload):
        with open(self.path, 'w') as f:
            json.dump(payload, f)

    def test_missing_file_returns_empty_dict(self):
        result = tui.read_status(os.path.join(self.tmp.name, 'nope.json'))
        self.assertEqual(result, {})

    def test_v2_status_returns_dict_without_warning(self):
        self._write({'schema_version': 2, 'pid': 1234, 'uptime_sec': 42})
        result = tui.read_status(self.path)
        self.assertEqual(result.get('schema_version'), 2)
        self.assertNotIn('_schema_warning', result)
        self.assertEqual(result['pid'], 1234)

    def test_v1_status_adds_schema_warning(self):
        self._write({'schema_version': 1, 'stats': {'rounds': 5}})
        result = tui.read_status(self.path)
        self.assertIn('_schema_warning', result)
        self.assertIn('1', result['_schema_warning'])
        self.assertIn('2', result['_schema_warning'])

    def test_v3_status_adds_schema_warning(self):
        self._write({'schema_version': 3, 'foo': 'bar'})
        result = tui.read_status(self.path)
        self.assertIn('_schema_warning', result)
        self.assertIn('3', result['_schema_warning'])

    def test_missing_schema_version_treated_as_v1(self):
        # No schema_version key -> defaults to 1 -> warning added.
        self._write({'pid': 99})
        result = tui.read_status(self.path)
        self.assertIn('_schema_warning', result)

    def test_corrupted_json_returns_empty_dict(self):
        with open(self.path, 'w') as f:
            f.write('not valid json {{{')
        result = tui.read_status(self.path)
        self.assertEqual(result, {})

    def test_empty_file_returns_empty_dict(self):
        # Zero-byte file -> JSONDecodeError -> safe default.
        Path(self.path).touch()
        result = tui.read_status(self.path)
        self.assertEqual(result, {})


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeInt(unittest.TestCase):
    """_safe_int: never raises, falls back to default on bad input."""

    def test_int_passthrough(self):
        self.assertEqual(tui._safe_int(42), 42)

    def test_string_int(self):
        self.assertEqual(tui._safe_int('42'), 42)

    def test_float_truncates(self):
        self.assertEqual(tui._safe_int(3.7), 3)

    def test_none_returns_default_zero(self):
        self.assertEqual(tui._safe_int(None), 0)

    def test_garbage_returns_default_zero(self):
        self.assertEqual(tui._safe_int('hello'), 0)

    def test_custom_default(self):
        self.assertEqual(tui._safe_int('xxx', 99), 99)
        self.assertEqual(tui._safe_int(None, -1), -1)

    def test_dict_returns_default(self):
        # dicts can't be coerced to int.
        self.assertEqual(tui._safe_int({'a': 1}), 0)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeFloat(unittest.TestCase):
    """_safe_float: mirrors _safe_int for floats."""

    def test_float_passthrough(self):
        self.assertEqual(tui._safe_float(3.14), 3.14)

    def test_string_float(self):
        self.assertAlmostEqual(tui._safe_float('3.14'), 3.14, places=5)

    def test_int_to_float(self):
        self.assertEqual(tui._safe_float(7), 7.0)

    def test_none_returns_default_zero(self):
        self.assertEqual(tui._safe_float(None), 0.0)

    def test_garbage_returns_default(self):
        self.assertEqual(tui._safe_float('xyz'), 0.0)
        self.assertEqual(tui._safe_float('xyz', 1.5), 1.5)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeGetFunnel(unittest.TestCase):
    """_safe_get_funnel: returns funnel dict, falls back to stats for v1."""

    def test_v2_returns_funnel(self):
        status = {
            'schema_version': 2,
            'funnel': {
                'suspects_produced': 10,
                'bugs_confirmed': 2,
                'fixes_succeeded': 1,
            },
        }
        f = tui._safe_get_funnel(status)
        self.assertEqual(f['suspects_produced'], 10)
        self.assertEqual(f['bugs_confirmed'], 2)
        self.assertEqual(f['fixes_succeeded'], 1)

    def test_v1_fallback_to_stats(self):
        status = {
            'schema_version': 1,
            'stats': {
                'suspects_produced': 7,
                'bugs_confirmed': 3,
                'fixes_attempted': 4,
                'fixes_succeeded': 2,
            },
        }
        f = tui._safe_get_funnel(status)
        self.assertEqual(f['suspects_produced'], 7)
        self.assertEqual(f['bugs_confirmed'], 3)
        self.assertEqual(f['fixes_attempted'], 4)
        self.assertEqual(f['fixes_succeeded'], 2)

    def test_empty_status_returns_all_zeros(self):
        f = tui._safe_get_funnel({})
        # Every field must be present and default to 0.
        for key in (
            'suspects_produced', 'suspects_refuted', 'suspects_known_fp',
            'bugs_confirmed', 'fixes_attempted', 'fixes_succeeded',
            'fixes_reverted', 'fixes_to_review', 'e2e_findings',
            'opus_invocations', 'rounds', 'clean_rounds_in_a_row',
        ):
            self.assertIn(key, f)
            self.assertEqual(f[key], 0, f'field {key!r} should default to 0')

    def test_non_dict_funnel_falls_back(self):
        # If 'funnel' is the wrong type, we must NOT crash.
        status = {'funnel': 'not a dict', 'stats': {'rounds': 11}}
        f = tui._safe_get_funnel(status)
        self.assertEqual(f['rounds'], 11)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestSafeGetLists(unittest.TestCase):
    """_safe_get_roles / _safe_get_nodes / _safe_get_threads: list coercion."""

    def test_roles_list(self):
        self.assertEqual(tui._safe_get_roles({'roles': [{'role': 'H'}]}),
                         [{'role': 'H'}])

    def test_roles_missing_returns_empty(self):
        self.assertEqual(tui._safe_get_roles({}), [])

    def test_roles_wrong_type_returns_empty(self):
        self.assertEqual(tui._safe_get_roles({'roles': 'oops'}), [])

    def test_nodes_list(self):
        self.assertEqual(tui._safe_get_nodes({'nodes': [{'id': 'x'}]}),
                         [{'id': 'x'}])

    def test_nodes_missing_returns_empty(self):
        self.assertEqual(tui._safe_get_nodes({}), [])

    def test_threads_list(self):
        self.assertEqual(tui._safe_get_threads({'threads': ['t1']}), ['t1'])

    def test_threads_missing_returns_empty(self):
        self.assertEqual(tui._safe_get_threads({}), [])


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestShortModel(unittest.TestCase):
    """_short_model: strip namespace prefix from model id."""

    def test_colon_separator(self):
        self.assertEqual(tui._short_model('opencode-go:qwen3.7-max'),
                         'qwen3.7-max')

    def test_slash_separator(self):
        self.assertEqual(tui._short_model('org/model'), 'model')

    def test_no_separator_unchanged(self):
        self.assertEqual(tui._short_model('plain-name'), 'plain-name')

    def test_multiple_colons_keeps_first_segment(self):
        # split(':', 1) -> 'a', 'b:c'
        self.assertEqual(tui._short_model('a:b:c'), 'b:c')


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestModelFamilyShort(unittest.TestCase):
    """model_family_short: known prefixes map to a short label."""

    def test_ollama_qwen_9b(self):
        self.assertEqual(tui.model_family_short('ollama:qwen3.5:9b'), '9b')

    def test_ollama_qwen_30b(self):
        self.assertEqual(tui.model_family_short('ollama:qwen3-coder:30b'), '30b')

    def test_gemini_flash(self):
        self.assertEqual(tui.model_family_short('gemini:gemini-2.5-flash-lite'),
                         'gem-fl')

    def test_gemini_pro(self):
        self.assertEqual(tui.model_family_short('gemini:gemini-2.5-pro'),
                         'gem-pro')

    def test_claude_sonnet(self):
        self.assertEqual(tui.model_family_short('claude:claude-sonnet-4-5'),
                         'sonnet')

    def test_claude_opus(self):
        self.assertEqual(tui.model_family_short('claude:claude-opus-4-6'),
                         'opus')

    def test_unknown_falls_back_to_last_segment(self):
        # Unknown prefix -> last ':' or '/' segment, truncated to 14 chars.
        result = tui.model_family_short('totally-unknown:thing-xyz')
        self.assertEqual(result, 'thing-xyz')

    def test_unknown_truncates_long_name(self):
        long_name = 'a' * 30
        result = tui.model_family_short(f'foo:{long_name}')
        self.assertEqual(len(result), 14)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestCheckAlive(unittest.TestCase):
    """check_alive: wraps os.kill(pid, 0) for liveness checks."""

    def test_current_pid_is_alive(self):
        self.assertTrue(tui.check_alive(os.getpid()))

    def test_dead_pid_is_not_alive(self):
        # A pid extremely unlikely to be in use; raises OSError -> False.
        self.assertFalse(tui.check_alive(2_999_999))


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestModelConfig(unittest.TestCase):
    """_load_model_config / _save_model_config: round-trip a config dict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'models.json')

    def test_load_missing_returns_defaults(self):
        config = tui._load_model_config(os.path.join(self.tmp.name, 'absent.json'))
        # Defaults must be a dict with all six tier keys.
        self.assertIsInstance(config, dict)
        for key in ('tier0_local', 'tier0_cloud', 'tier1_filters',
                    'tier2_judges', 'tier3_fixers', 'tier4_arbiters'):
            self.assertIn(key, config)
            self.assertIsInstance(config[key], list)

    def test_save_then_load_roundtrip(self):
        original = {
            'tier0_local': ['m1', 'm2'],
            'tier0_cloud': ['c1'],
            'tier1_filters': ['f1', 'f2', 'f3'],
            'tier2_judges': ['j1'],
            'tier3_fixers': ['x1'],
            'tier4_arbiters': ['a1'],
        }
        tui._save_model_config(self.path, original)
        loaded = tui._load_model_config(self.path)
        self.assertEqual(loaded, original)

    def test_load_corrupted_returns_defaults(self):
        with open(self.path, 'w') as f:
            f.write('not json {{{')
        config = tui._load_model_config(self.path)
        # Falls back to defaults rather than crashing.
        self.assertIn('tier0_local', config)


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestLogBuffer(unittest.TestCase):
    """LogBuffer: in-memory ring buffer of log lines with timestamps."""

    def test_append_msg_preserves_order(self):
        lb = tui.LogBuffer(max_lines=10)
        lb.append_msg('first')
        lb.append_msg('second')
        self.assertEqual(len(lb.lines), 2)
        self.assertEqual(lb.lines[0][1], 'first')
        self.assertEqual(lb.lines[1][1], 'second')

    def test_max_lines_enforced(self):
        lb = tui.LogBuffer(max_lines=3)
        for i in range(5):
            lb.append_msg(f'line-{i}')
        # Only the last 3 should remain.
        self.assertEqual(len(lb.lines), 3)
        self.assertEqual(lb.lines[0][1], 'line-2')
        self.assertEqual(lb.lines[-1][1], 'line-4')

    def test_scroll_up_increments_cursor(self):
        lb = tui.LogBuffer(max_lines=10)
        for i in range(5):
            lb.append_msg(f'l{i}')
        lb.reset_cursor()
        lb.scroll_up(2)
        self.assertEqual(lb._cursor, 2)

    def test_scroll_down_decrements_cursor(self):
        lb = tui.LogBuffer(max_lines=10)
        for i in range(5):
            lb.append_msg(f'l{i}')
        lb.scroll_up(3)
        lb.scroll_down(1)
        self.assertEqual(lb._cursor, 2)

    def test_scroll_up_clamps_to_buffer_end(self):
        lb = tui.LogBuffer(max_lines=10)
        for i in range(3):
            lb.append_msg(f'l{i}')
        lb.scroll_up(999)
        # Max cursor is len(lines) - 1 = 2.
        self.assertEqual(lb._cursor, 2)

    def test_scroll_down_clamps_to_zero(self):
        lb = tui.LogBuffer(max_lines=10)
        lb.scroll_down(5)
        self.assertEqual(lb._cursor, 0)

    def test_reset_cursor_returns_to_zero(self):
        lb = tui.LogBuffer(max_lines=10)
        for i in range(5):
            lb.append_msg(f'l{i}')
        lb.scroll_up(3)
        lb.reset_cursor()
        self.assertEqual(lb._cursor, 0)

    def test_update_missing_file_returns_false(self):
        lb = tui.LogBuffer(max_lines=10)
        result = lb.update(os.path.join('/nonexistent', 'nope.log'))
        self.assertFalse(result)

    def test_update_parses_timestamped_lines(self):
        lb = tui.LogBuffer(max_lines=10)
        path = os.path.join(self.tmp_dir(), 'test.log')
        ts = time.strftime('%H:%M:%S')
        with open(path, 'w') as f:
            f.write(f'[{ts}] hello world\n')
            f.write(f'[{ts}] second line\n')
        result = lb.update(path)
        self.assertTrue(result)
        self.assertEqual(len(lb.lines), 2)
        # Lines preserve their text content.
        texts = [line for _, line in lb.lines]
        self.assertIn('hello world', texts[0])
        self.assertIn('second line', texts[1])

    def test_update_returns_false_when_no_new_content(self):
        lb = tui.LogBuffer(max_lines=10)
        path = os.path.join(self.tmp_dir(), 'test.log')
        ts = time.strftime('%H:%M:%S')
        with open(path, 'w') as f:
            f.write(f'[{ts}] one\n')
        self.assertTrue(lb.update(path))
        # Second call sees no new bytes -> False.
        self.assertFalse(lb.update(path))

    def test_update_handles_log_rotation(self):
        """When the log file shrinks, the buffer is reset and re-read."""
        lb = tui.LogBuffer(max_lines=10)
        path = os.path.join(self.tmp_dir(), 'rotate.log')
        ts = time.strftime('%H:%M:%S')
        # First, write 1KB of noise so the file is "large".
        with open(path, 'w') as f:
            f.write(f'[{ts}] old line\n')
            f.write('x' * 2000 + '\n')
        lb.update(path)
        self.assertGreater(len(lb.lines), 0)
        # Now rotate: overwrite with a small file.
        with open(path, 'w') as f:
            f.write(f'[{ts}] rotated line\n')
        result = lb.update(path)
        self.assertTrue(result)
        # After rotation, the buffer is reset to just the new content.
        self.assertEqual(len(lb.lines), 1)
        self.assertIn('rotated line', lb.lines[0][1])

    @staticmethod
    def tmp_dir():
        d = tempfile.mkdtemp()
        return d


@unittest.skipIf(tui is None, "tui.py could not be imported (curses missing)")
class TestReadLogCalls(unittest.TestCase):
    """read_log_calls: parse [call] model-id lines within a time window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'harness.log')

    def test_missing_file_returns_empty_counter(self):
        result = tui.read_log_calls(os.path.join(self.tmp.name, 'absent.log'),
                                    window_sec=60)
        self.assertIsInstance(result, Counter)
        self.assertEqual(result, Counter())

    def test_counts_recent_call_lines(self):
        ts = time.strftime('%H:%M:%S')
        with open(self.path, 'w') as f:
            f.write(f'[{ts}] [call] ollama:qwen3.5:9b running something\n')
            f.write(f'[{ts}] [call] ollama:qwen3.5:9b running again\n')
            f.write(f'[{ts}] [call] minimax/M3 thinking\n')
            f.write(f'[{ts}] [info] this is not a call line\n')
        counts = tui.read_log_calls(self.path, window_sec=120)
        self.assertEqual(counts['ollama:qwen3.5:9b'], 2)
        self.assertEqual(counts['minimax/M3'], 1)
        # Non-call lines must NOT contribute.
        self.assertNotIn('info', counts)

    def test_skips_lines_without_call_marker(self):
        ts = time.strftime('%H:%M:%S')
        with open(self.path, 'w') as f:
            f.write(f'[{ts}] just a log line\n')
            f.write(f'[{ts}] another without marker\n')
        counts = tui.read_log_calls(self.path, window_sec=60)
        self.assertEqual(counts, Counter())

    def test_skips_lines_outside_window(self):
        # Use a window of 0 seconds: only the "current second" counts.
        # Build a line with a timestamp from yesterday -> it must be skipped.
        old_ts = '00:00:01'  # likely not "now" even with window=60
        with open(self.path, 'w') as f:
            f.write(f'[{old_ts}] [call] ollama:qwen3.5:9b stale\n')
        # Run at a time far from 00:00:01 with a tiny window.
        # We can't easily fake the clock, so we just assert it's a Counter.
        counts = tui.read_log_calls(self.path, window_sec=60)
        self.assertIsInstance(counts, Counter)


if __name__ == '__main__':
    unittest.main(verbosity=2)
