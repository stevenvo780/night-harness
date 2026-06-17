#!/usr/bin/env python3
"""Tests del parser de bugs del hunter (_parse_bug_line) — A-PR2.

Garantiza el contrato JSONL nuevo y el fallback robusto que des-duplica el
caso histórico snippet==why con `::` literales.

Correr:  python3 tests/test_parser.py   (o pytest tests/)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestParseBugLine(unittest.TestCase):
    def test_json_contract(self):
        r = harness._parse_bug_line(
            '{"file":"lib/x.ts","line":42,"severity":"high",'
            '"snippet":"const a = b","why":"falta scope tenantId","repro":"abrir /x"}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'lib/x.ts')
        self.assertEqual(r['line'], 42)
        self.assertEqual(r['severity'], 'high')
        self.assertEqual(r['snippet'], 'const a = b')
        self.assertEqual(r['why'], 'falta scope tenantId')
        self.assertEqual(r['repro'], 'abrir /x')
        # Invariante clave: snippet (código) != why (prosa).
        self.assertNotEqual(r['snippet'], r['why'])

    def test_json_missing_fields_defaults(self):
        r = harness._parse_bug_line('{"file":"a.ts","line":1,"why":"x"}')
        self.assertIsNotNone(r)
        self.assertEqual(r['severity'], 'medium')
        self.assertEqual(r['snippet'], '')
        self.assertEqual(r['repro'], '')

    def test_legacy_double_colon_dedup(self):
        # El caso real del log: snippet y why quedaban idénticos con `::` literal.
        line = ('app/(panel)/caja/CajaTabs.tsx:105 const activo = t.id===tab;'
                '::Posible inconsistencia en la lógica de pestañas')
        r = harness._parse_bug_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'app/(panel)/caja/CajaTabs.tsx')
        self.assertEqual(r['line'], 105)
        self.assertEqual(r['snippet'], 'const activo = t.id===tab;')
        self.assertTrue(r['why'].startswith('Posible inconsistencia'))
        # Invariantes del fix: sin `::` literal y snippet != why.
        self.assertNotIn('::', r['snippet'])
        self.assertNotIn('::', r['why'])
        self.assertNotEqual(r['snippet'], r['why'])

    def test_legacy_dash_separator(self):
        r = harness._parse_bug_line('lib/queries/clientes.ts:42 - falta filtro tenantId')
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'lib/queries/clientes.ts')
        self.assertEqual(r['line'], 42)
        self.assertEqual(r['why'], 'falta filtro tenantId')

    def test_markdown_bullet(self):
        r = harness._parse_bug_line('- `app/(panel)/admin/page.tsx:37`: sin guard de rol admin')
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'app/(panel)/admin/page.tsx')
        self.assertEqual(r['line'], 37)

    def test_rejects_non_bug_lines(self):
        for junk in ('', '   ', 'NO_BUGS', '# comentario', 'esto no tiene path:linea',
                     'fecha:105::sin extension de archivo'):
            self.assertIsNone(harness._parse_bug_line(junk), f'debería rechazar: {junk!r}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
