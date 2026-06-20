"""Parsing helpers for model-produced bug reports."""
from __future__ import annotations

import json
import re
from typing import Optional


def parse_bug_line(line: str) -> Optional[dict]:
    """Extract a normalized bug dict from one hunter output line."""
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('NO_BUGS'):
        return None

    if line.startswith('{') and line.endswith('}'):
        try:
            d = json.loads(line)
            f = str(d.get('file', '')).strip()
            if f and ('line' in d):
                return {
                    'file': f,
                    'line': int(d.get('line', 0) or 0),
                    'severity': str(d.get('severity', 'medium')).lower(),
                    'snippet': str(d.get('snippet', ''))[:200],
                    'why': str(d.get('why', ''))[:500],
                    'repro': str(d.get('repro', ''))[:300],
                }
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    path = r'[\w/.\-()]+\.[a-z]{1,5}'
    match = re.match(
        rf'^[\s\-\*`]*(?P<file>{path}):(?P<line>\d+)`?'
        r'\s*[:\-–—]*\s*(?P<rest>.{5,800})$',
        line,
    )
    if not match:
        return None

    rest = match.group('rest').strip().strip('`')
    if '::' in rest:
        parts = [p.strip() for p in rest.split('::') if p.strip()]
        snippet = parts[0][:200] if parts else ''
        why = (parts[-1] if len(parts) > 1 else parts[0])[:500] if parts else ''
    else:
        snippet, why = '', rest[:500]

    return {
        'file': match.group('file'),
        'line': int(match.group('line')),
        'severity': 'medium',
        'snippet': snippet,
        'why': why,
        'repro': '',
    }

