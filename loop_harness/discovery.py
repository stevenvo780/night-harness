"""Code-zone discovery for the harness worktree."""
from __future__ import annotations

from pathlib import Path


def discover_zones(*, wt: Path, subdir: str) -> list[str]:
    """Discover panel route zones under the App Router."""
    app_dir = wt / subdir / 'app' / '(panel)'
    if not app_dir.exists():
        return []
    zones = []
    for entry in sorted(app_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith('('):
            zones.append(f'app/(panel)/{entry.name}/')
    return zones


def discover_code_zones(*, wt: Path, subdir: str) -> list[list[str]]:
    """Group TS/TSX files by route/lib zone for hunters."""
    app_dir = wt / subdir / 'app' / '(panel)'
    lib_dir = wt / subdir / 'lib'
    groups: dict[str, list[str]] = {}

    if app_dir.exists():
        for entry in sorted(app_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith('('):
                zone = f'app/(panel)/{entry.name}/'
                files = []
                for tsx in entry.rglob('*.tsx'):
                    files.append(str(tsx.relative_to(wt / subdir)))
                for ts in entry.rglob('*.ts'):
                    if not ts.name.endswith('.test.ts'):
                        files.append(str(ts.relative_to(wt / subdir)))
                if files:
                    groups[zone] = files[:30]

    if lib_dir.exists():
        for ts in lib_dir.rglob('*.ts'):
            rel = str(ts.relative_to(wt / subdir))
            if '.test.' in rel:
                continue
            prefix = '/'.join(rel.split('/')[:2]) + '/'
            groups.setdefault(prefix, []).append(rel)
            if len(groups[prefix]) > 30:
                groups[prefix] = groups[prefix][:30]

    return list(groups.values())

