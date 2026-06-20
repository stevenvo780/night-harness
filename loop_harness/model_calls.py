"""Model invocation helpers for local Ollama and cloud CLIs."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Optional


_OLLAMA_SEMAPHORES: dict[str, threading.Semaphore] = {}
_OLLAMA_SEM_GUARD = threading.Lock()
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')


def cli_cwd(wt_subdir: Path) -> Optional[str]:
    """Return a CLI working directory if the target worktree exists."""
    return str(wt_subdir) if wt_subdir.is_dir() else None


def is_cpu_model(model_name: str) -> bool:
    name = model_name.lower()
    return 'cpu' in name or '80b' in name or 'next' in name


def ollama_sem(model_name: str) -> threading.Semaphore:
    """Return the per-model Ollama concurrency semaphore."""
    with _OLLAMA_SEM_GUARD:
        sem = _OLLAMA_SEMAPHORES.get(model_name)
        if sem is None:
            limit = 1 if is_cpu_model(model_name) else int(
                os.environ.get('LOOP_OLLAMA_GPU_CONC', '2'))
            sem = threading.Semaphore(limit)
            _OLLAMA_SEMAPHORES[model_name] = sem
        return sem


def call_model(
    spec,
    system: str,
    prompt: str,
    *,
    max_tokens: int,
    timeout: int,
    quota_module,
    is_healthy: Callable[[str], bool],
    mark_health: Callable[[str, bool], None],
    log: Callable[[str], None],
    call_ollama: Callable[..., str],
    call_minimax: Callable[..., str],
    call_opencode: Callable[..., str],
    call_gemini_cli: Callable[..., str],
    call_claude_cli: Callable[..., str],
    call_codex_cli: Callable[..., str],
) -> Optional[str]:
    """Dispatch one model call and update quota/health accounting."""
    if not quota_module.is_model_ok(spec.id):
        log(f'  [quota] {spec.id} bloqueado por cuota')
        return None
    if not is_healthy(spec.id):
        return None

    t0 = time.time()
    text: Optional[str] = None
    in_tok = 0
    out_tok = 0

    try:
        if spec.id.startswith('ollama:'):
            text = call_ollama(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('minimax/') or spec.id.startswith('minimax:'):
            text = call_minimax(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('opencode:') or spec.id.startswith('opencode-go:'):
            text = call_opencode(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('gemini:'):
            text = call_gemini_cli(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('claude:'):
            text = call_claude_cli(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('codex:'):
            text = call_codex_cli(spec, prompt, system, max_tokens, timeout)
        else:
            log(f'  [call] modelo desconocido: {spec.id}')
            return None

        if text is not None:
            in_tok = max(1, (len(system) + len(prompt)) // 4)
            out_tok = max(1, len(text) // 4)
    except subprocess.TimeoutExpired:
        log(f'  [call] {spec.id} timeout {timeout}s')
    except Exception as exc:
        log(f'  [call] {spec.id} error: {exc}')

    mark_health(spec.id, bool(text and text.strip()))
    quota_module.record(spec.id, in_tok, out_tok)
    elapsed = time.time() - t0
    log(f'  [call] {spec.id} {elapsed:.1f}s  in~{in_tok}  out~{out_tok}')
    return text


def call_ollama(spec, prompt: str, system: str, max_tokens: int, timeout: int) -> str:
    """Call the Ollama HTTP generate endpoint."""
    model_name = spec.id.split(':', 1)[1]
    base = getattr(spec, 'base_url', None) or (
        os.environ.get('LOOP_OLLAMA_BASE') or 'http://host.docker.internal:11434')
    num_ctx = getattr(spec, 'num_ctx', None) or int(
        os.environ.get('LOOP_OLLAMA_NUM_CTX', '8192'))
    options = {'num_predict': max_tokens, 'temperature': 0.2, 'num_ctx': num_ctx}
    gpu_index = getattr(spec, 'gpu_index', None)
    if gpu_index is not None:
        options['main_gpu'] = gpu_index
    payload = {
        'model': model_name,
        'prompt': prompt,
        'system': system,
        'stream': False,
        'keep_alive': os.environ.get('LOOP_OLLAMA_KEEP_ALIVE', '10m'),
        'options': options,
    }
    actual_timeout = max(timeout, 300) if is_cpu_model(model_name) else timeout
    req = urllib.request.Request(
        f'{base}/api/generate',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with ollama_sem(model_name):
        with urllib.request.urlopen(req, timeout=actual_timeout) as response:
            data = json.loads(response.read())
    return data.get('response', '').strip()


def clean_cli_out(stdout: str) -> str:
    """Strip ANSI escapes and known opencode-go prelude lines."""
    out_lines = []
    for line in (stdout or '').split('\n'):
        line = _ANSI_RE.sub('', line)
        if line.startswith('[0m') or 'build ·' in line:
            continue
        out_lines.append(line)
    return '\n'.join(out_lines).strip()


def call_cli(
    cmd: list[str],
    prompt: str,
    system: str,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
    name: str = 'cli',
    min_timeout: int = 0,
    on_stderr=None,
) -> str:
    """Run a model CLI command with unified timeout/error handling."""
    actual = max(timeout, min_timeout) if min_timeout else timeout
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=actual,
            cwd=cwd(),
        )
    except FileNotFoundError as exc:
        log(f'  [{name}] CLI no encontrado en PATH: {exc}')
        return ''
    except subprocess.TimeoutExpired:
        log(f'  [{name}] timeout {actual}s')
        return ''
    if on_stderr is not None and result.stderr:
        try:
            on_stderr(result.stderr, result.returncode)
        except Exception:
            pass
    return clean_cli_out(result.stdout)


def call_minimax(
    spec,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
) -> str:
    """Call Minimax through opencode, with direct HTTP fallback."""
    model_id = spec.id
    full_prompt = (f'<<SYS>>\n{system}\n<</SYS>>\n\n{prompt}' if system else prompt)
    try:
        result = subprocess.run(
            ['opencode', 'run', '--model', model_id, full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd(),
        )
        out = clean_cli_out(result.stdout)
        if not out and result.stderr:
            log(f'  [minimax] stderr: {result.stderr[:200]}')
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f'  [minimax] CLI fallo: {exc}; probando HTTP directo')
        model_name = spec.id.split('/', 1)[-1]
        return call_minimax_http(model_name, full_prompt, max_tokens, timeout)


def call_minimax_http(model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = os.environ.get('MINIMAX_API_KEY', '')
    if not api_key:
        return ''
    base = os.environ.get('MINIMAX_API_BASE', 'https://api.minimax.io/v1')
    req = urllib.request.Request(
        f'{base}/chat/completions',
        data=json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
        }).encode(),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read())
    return data['choices'][0]['message']['content'].strip()


def call_opencode(
    spec,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
) -> str:
    opencode_model_id = spec.id.replace(':', '/', 1)
    full_prompt = (f'<<SYS>>\n{system}\n<</SYS>>\n\n{prompt}' if system else prompt)
    return call_cli(
        ['opencode', 'run', '--model', opencode_model_id, full_prompt],
        prompt,
        system,
        timeout,
        cwd=cwd,
        log=log,
        name='opencode',
        min_timeout=180,
    )


def call_claude_cli(
    spec,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
) -> str:
    model = spec.id.split(':', 1)[1]
    full_prompt = f'<system>{system}</system>\n\n{prompt}' if system else prompt
    return call_cli(
        ['claude', '-p', full_prompt, '--model', model, '--output-format', 'text'],
        prompt,
        system,
        timeout,
        cwd=cwd,
        log=log,
        name='claude',
    )


def call_gemini_cli(
    spec,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
) -> str:
    model = spec.id.split(':', 1)[1]
    full_prompt = f'{system}\n\n{prompt}' if system else prompt

    def _check(stderr, returncode):
        if returncode != 0 and '429' in stderr:
            log(f'  [gemini] {model} rate-limited (429)')

    return call_cli(
        ['gemini', '-m', model, '-p', full_prompt, '--yolo', '--output-format', 'text'],
        prompt,
        system,
        timeout,
        cwd=cwd,
        log=log,
        name='gemini',
        on_stderr=_check,
    )


def call_codex_cli(
    spec,
    prompt: str,
    system: str,
    max_tokens: int,
    timeout: int,
    *,
    cwd: Callable[[], Optional[str]],
    log: Callable[[str], None],
) -> str:
    model = spec.id.split(':', 1)[1] if ':' in spec.id else 'gpt-5.5'
    full_prompt = f'{system}\n\n{prompt}' if system else prompt
    return call_cli(
        ['codex', 'exec', '--model', model, '--skip-git-repo-check', full_prompt],
        prompt,
        system,
        timeout,
        cwd=cwd,
        log=log,
        name='codex',
    )

