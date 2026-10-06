"""Validation, patch and E2E gate helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Optional


def looks_like_patch(text: str) -> bool:
    """Return True when text looks like a unified diff."""
    stripped = text.lstrip()
    return (
        stripped.startswith(('--- ', 'diff ', '@@ '))
        or ('--- ' in text and '+++ ' in text and '@@' in text)
    )


def patch_files(diff_text: str) -> set[str]:
    """Extract touched file paths from a unified diff."""
    files = set()
    old_remaining = new_remaining = 0
    for line in diff_text.splitlines():
        if old_remaining or new_remaining:
            # Hunk content can look like a file header (e.g. -- i; -> ++ i;).
            if line.startswith((' ', '-')):
                old_remaining -= 1
            if line.startswith((' ', '+')):
                new_remaining -= 1
            continue

        hunk = re.match(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@', line)
        if hunk:
            old_remaining = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_remaining = int(hunk.group(2)) if hunk.group(2) is not None else 1
            continue

        if line.startswith(('+++ ', '--- ')):
            path = line[4:].split('\t', 1)[0].strip()
            if path and path != '/dev/null':
                files.add(re.sub(r'^[ab]/', '', path))
    return files


def apply_patch(diff_text: str, *, wt: Path, log: Callable[[str], None]) -> bool:
    """Apply a unified diff inside the target worktree."""
    try:
        result = subprocess.run(
            ['git', 'apply', '--3way', '--whitespace=nowarn', '-'],
            input=diff_text,
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log(f'  [fix] git apply fallo: {(result.stderr or "")[:160]}')
        return result.returncode == 0
    except Exception as exc:
        log(f'  [fix] git apply excepcion: {exc}')
        return False


def revert_patch(diff_text: str, *, wt: Path, log: Callable[[str], None]) -> bool:
    """Revert a previously applied unified diff."""
    try:
        result = subprocess.run(
            ['git', 'apply', '-R', '--3way', '--whitespace=nowarn', '-'],
            input=diff_text,
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log(f'  [fix] git apply -R fallo: {(result.stderr or "")[:160]}')
        return result.returncode == 0
    except Exception as exc:
        log(f'  [fix] git apply -R excepcion: {exc}')
        return False


def apply_and_validate(
    path: Path,
    new_content: str,
    suspect,
    *,
    subdir: str,
    log: Callable[[str], None],
    looks_like_patch_fn: Callable[[str], bool],
    patch_files_fn: Callable[[str], set[str]],
    apply_patch_fn: Callable[[str], bool],
    revert_patch_fn: Callable[[str], bool],
    run_tsc_fn: Callable[[], bool],
    run_smoke_subset_fn: Callable[[str], bool],
) -> bool:
    """Apply full-file content or a patch, then run typecheck/E2E gates."""
    if looks_like_patch_fn(new_content):
        touched = patch_files_fn(new_content)
        if len(touched) > 1:
            log(f'  [fix] patch multi-archivo rechazado: {sorted(touched)}')
            return False
        if not apply_patch_fn(new_content):
            return False
        expected = {suspect.file}
        if subdir not in ('', '.'):
            expected.add(str(Path(subdir) / suspect.file))
        if not (touched & expected):
            log(f'  [fix] patch no toca {suspect.file}; patch cubre {sorted(touched)}')
            revert_patch_fn(new_content)
            return False
    else:
        try:
            path.write_text(new_content)
        except OSError as exc:
            log(f'  [fix] no se pudo escribir: {exc}')
            return False

    if not run_tsc_fn():
        log('  [fix] tsc fallo')
        return False
    if not run_smoke_subset_fn(suspect.file):
        log('  [fix] E2E gate fallo')
        return False
    return True


def run_tsc(*, wt: Path, subdir: str) -> bool:
    """Run TypeScript typecheck in the target subdir."""
    try:
        result = subprocess.run(
            ['npx', 'tsc', '--noEmit'],
            cwd=str(wt / subdir),
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def route_for_file(file_path: str) -> Optional[str]:
    """Derive a URL route from an app-router file path."""
    if not file_path:
        return None
    match = re.search(r'app/\(panel\)/([^/]+)/', file_path)
    if match:
        return '/' + match.group(1)
    match = re.search(r'app/([^/(][^/]*)/', file_path)
    return '/' + match.group(1) if match else None


def dev_server_up(dev_url: str) -> bool:
    try:
        with urllib.request.urlopen(dev_url + '/', timeout=8) as response:
            return response.status < 500
    except Exception:
        return False


def run_e2e_route(
    route: str,
    *,
    dev_url: str,
    storage: str,
    wt_subdir: Path,
    runner: Path,
    max_actions: int = 12,
    budget_ms: int = 45000,
    timeout: int = 90,
    log: Callable[[str], None],
) -> tuple[bool, list]:
    """Run the Node E2E crawler for one route."""
    scenario = {
        'baseURL': dev_url,
        'route': route,
        'maxActions': max_actions,
        'budgetMs': budget_ms,
    }
    if storage:
        scenario['storageState'] = storage
    env = dict(os.environ)
    env.setdefault('DASHBOARD_NM', str(wt_subdir / 'node_modules'))
    try:
        result = subprocess.run(
            ['node', str(runner)],
            input=json.dumps(scenario),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(wt_subdir),
            env=env,
        )
        out = json.loads(result.stdout)
    except Exception as exc:
        log(f'  [fix] E2E no corrio ({route}): {exc}')
        return (False, [])
    if out.get('fatal'):
        log(f'  [fix] E2E fatal ({route}): {str(out.get("fatal"))[:120]}')
        return (False, [])
    fatal = [
        err for err in out.get('errors', [])
        if err.get('kind') in ('http', 'pageerror', 'errorUI', 'console')
    ]
    return (True, fatal)


def run_smoke_subset(
    file_path: str,
    *,
    dev_url: str,
    log: Callable[[str], None],
    is_risky_path: Callable[[str], bool],
    dev_server_up_fn: Callable[[], bool],
    route_for_file_fn: Callable[[str], Optional[str]],
    run_e2e_route_fn: Callable[[str], tuple[bool, list]],
) -> bool:
    """Run the targeted smoke gate for a touched file."""
    env_strict = os.environ.get('LOOP_GATE_REQUIRE_E2E')
    if env_strict is not None:
        strict = env_strict == '1'
    else:
        strict = is_risky_path(file_path)

    try:
        server_up = dev_server_up_fn()
    except Exception:
        server_up = False
    if not server_up:
        if strict:
            log('  [fix] gate estricto: dev server caido -> NO commitear')
            return False
        log('  [fix] dev server caido -> gate tsc-only')
        return True

    route = route_for_file_fn(file_path)
    if not route:
        return True
    ran, fatal = run_e2e_route_fn(route)
    if not ran:
        if strict:
            log(f'  [fix] E2E no corrio en {route} (estricto) -> NO commitear')
            return False
        log(f'  [fix] E2E no corrio en {route} -> tsc-only')
        return True
    if fatal:
        log(f'  [fix] E2E {route}: {len(fatal)} fatales '
            f'{[f.get("kind") for f in fatal]} -> revertir')
        return False
    log(f'  [fix] E2E {route}: limpio')
    return True


def commit_fix(path: Path, suspect, *, wt: Path, log: Callable[[str], None]) -> Optional[str]:
    """Commit one validated fix in the isolated worktree."""
    rel = path.relative_to(wt)
    try:
        subprocess.run(['git', 'add', str(rel)], cwd=str(wt), check=True, capture_output=True)
        msg = (
            f'fix(harness-v7): {suspect.file}:{suspect.line} - {suspect.why[:60]}\n\n'
            f'Generado por harness v7.\n'
            f'Hash: {suspect.hash}'
        )
        subprocess.run(
            ['git', 'commit', '-m', msg],
            cwd=str(wt),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        rev = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return rev.stdout.strip()
    except subprocess.CalledProcessError as exc:
        log(f'  [fix] git fallo: {exc.stderr[:200] if exc.stderr else exc}')
        return None


def map_e2e_route_to_file(route: str, *, wt_subdir: Path, log: Callable[[str], None]) -> Optional[str]:
    """Map a crawled route to a likely source file."""
    route = route.strip('/').lower()
    if not route:
        return 'app/page.tsx'
    candidates = [
        f'app/(panel)/{route}/page.tsx',
        f'app/{route}/page.tsx',
        f'pages/{route}.tsx',
        f'pages/{route}/index.tsx',
    ]
    for candidate in candidates:
        if (wt_subdir / candidate).exists():
            return candidate
    log(f'    [e2e-map] {route} -> no encontrado en {candidates}')
    return None


def dev_server_alive(dev_url: str) -> bool:
    """Cheap liveness check for the dev server."""
    try:
        req = urllib.request.Request(dev_url, method='HEAD')
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status < 600
    except Exception:
        return False

