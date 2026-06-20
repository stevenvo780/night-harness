#!/usr/bin/env python3
"""
memory.py — Memoria persistente del Harness v6.

Aprende de cada sesión y persiste entre sesiones en `memory.json`. Tres
funciones principales:

  · `record_bug(hash, info)`    — añade un bug confirmado al catálogo. Si ya
                                  existe, actualiza el contador.
  · `record_false_positive(hash)` — añade un patrón que el 9b marcó como
                                  bug pero NO era. El hunter lo filtra en
                                  futuras sesiones.
  · `is_hardened(zone)`         — True si una zona del código ya fue
                                  endurecida en N rondas limpias; el hunter
                                  la prioriza menos.

El hash es estable: usa el archivo + línea + 8 palabras del contexto para
que se mantenga estable entre runs aunque el código cambie ligeramente.

API:
  MEM = Memory.load()           — carga o crea
  MEM.save()                    — persiste
  MEM.record_bug(...)
  MEM.record_false_positive(...)
  MEM.is_hardened(zone)
  MEM.should_skip(suspect)      — ¿descartar sospechoso (falso positivo conocido)?
  MEM.summary()                 — texto para report
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

MEMORY_PATH = Path(os.environ.get(
    'LOOP_MEMORY',
    '/tmp/loop-memory.json',
))


def hash_suspect(file: str, line: int, snippet: str) -> str:
    """Hash estable de un sospechoso: archivo + línea + palabras del snippet."""
    text = f'{file}::{line}::' + ' '.join(snippet.split()[:8])
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _quarantine_corrupt(path: Path, exc: Exception) -> None:
    """Archiva un memory.json ilegible en `<name>.corrupt` y avisa por stderr,
    en vez de borrar el historial en silencio (p.ej. tras un cambio de schema)."""
    try:
        corrupt = path.with_name(path.name + '.corrupt')
        os.replace(path, corrupt)
        print(f'[memory] {path} corrupto ({exc}); archivado en {corrupt}', file=sys.stderr)
    except OSError:
        pass


@dataclass
class BugRecord:
    hash: str
    file: str
    line: int
    snippet: str
    first_seen: float
    last_seen: float
    times_confirmed: int = 0
    fixed: bool = False
    fix_commit: Optional[str] = None


@dataclass
class FalsePositive:
    hash: str
    file: str
    line: int
    snippet: str
    first_seen: float
    times_seen: int = 1
    reason: str = ''
    last_seen: float = 0.0


@dataclass
class ZoneHardness:
    """Cuántas rondas limpias ha tenido una zona. Si >= 3, se considera endurecida."""
    file: str
    clean_rounds: int = 0
    last_clean: float = 0.0


@dataclass
class Memory:
    bugs: dict[str, BugRecord] = field(default_factory=dict)
    false_positives: dict[str, FalsePositive] = field(default_factory=dict)
    zones: dict[str, ZoneHardness] = field(default_factory=dict)
    """Clave: path de archivo (o prefijo de directorio, ej. 'app/(panel)/clientes/')."""

    # Métricas globales.
    total_rounds: int = 0
    total_bugs_found: int = 0
    total_fixes_applied: int = 0
    last_session_at: float = 0.0
    last_session_summary: str = ''

    # ---- Persistencia ----
    @classmethod
    def load(cls, path: Path = MEMORY_PATH) -> 'Memory':
        if not path.exists():
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError('memory JSON root no es un objeto')
            mem = cls()
            mem.bugs = {k: BugRecord(**v) for k, v in data.get('bugs', {}).items()}
            mem.false_positives = {
                k: FalsePositive(**v) for k, v in data.get('false_positives', {}).items()
            }
            mem.zones = {
                k: ZoneHardness(**v) for k, v in data.get('zones', {}).items()
            }
            mem.total_rounds = data.get('total_rounds', 0)
            mem.total_bugs_found = data.get('total_bugs_found', 0)
            mem.total_fixes_applied = data.get('total_fixes_applied', 0)
            mem.last_session_at = data.get('last_session_at', 0.0)
            mem.last_session_summary = data.get('last_session_summary', '')
            return mem
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            # No perder el historial en silencio: archivar el corrupto y seguir.
            _quarantine_corrupt(path, exc)
            return cls()

    def save(self, path: Path = MEMORY_PATH) -> None:
        data = {
            'bugs': {k: asdict(v) for k, v in self.bugs.items()},
            'false_positives': {k: asdict(v) for k, v in self.false_positives.items()},
            'zones': {k: asdict(v) for k, v in self.zones.items()},
            'total_rounds': self.total_rounds,
            'total_bugs_found': self.total_bugs_found,
            'total_fixes_applied': self.total_fixes_applied,
            'last_session_at': self.last_session_at,
            'last_session_summary': self.last_session_summary,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    # ---- Bugs ----
    def record_bug(
        self,
        file: str,
        line: int,
        snippet: str,
        fix_commit: Optional[str] = None,
    ) -> BugRecord:
        h = hash_suspect(file, line, snippet)
        now = time.time()
        if h in self.bugs:
            rec = self.bugs[h]
            rec.last_seen = now
            rec.times_confirmed += 1
            if fix_commit:
                rec.fixed = True
                rec.fix_commit = fix_commit
                self.total_fixes_applied += 1
        else:
            rec = BugRecord(
                hash=h,
                file=file,
                line=line,
                snippet=snippet[:300],
                first_seen=now,
                last_seen=now,
                times_confirmed=1,
                fixed=bool(fix_commit),
                fix_commit=fix_commit,
            )
            self.bugs[h] = rec
            self.total_bugs_found += 1
        return rec

    def get_bug(self, file: str, line: int, snippet: str) -> Optional[BugRecord]:
        h = hash_suspect(file, line, snippet)
        return self.bugs.get(h)

    def open_bugs(self) -> list[BugRecord]:
        return [b for b in self.bugs.values() if not b.fixed]

    # ---- Falsos positivos ----
    def record_false_positive(
        self, file: str, line: int, snippet: str, reason: str = ''
    ) -> FalsePositive:
        h = hash_suspect(file, line, snippet)
        now = time.time()
        if h in self.false_positives:
            rec = self.false_positives[h]
            rec.times_seen += 1
            rec.last_seen = now
        else:
            rec = FalsePositive(
                hash=h,
                file=file,
                line=line,
                snippet=snippet[:300],
                first_seen=now,
                last_seen=now,
                reason=reason,
            )
            self.false_positives[h] = rec
        return rec

    def should_skip(self, file: str, line: int, snippet: str) -> bool:
        """True si este sospechoso es un falso positivo conocido."""
        h = hash_suspect(file, line, snippet)
        if h in self.false_positives:
            return self.false_positives[h].times_seen >= 2
        return False

    # ---- Endurecimiento de zonas ----
    def mark_zone_clean(self, file: str) -> None:
        now = time.time()
        if file not in self.zones:
            self.zones[file] = ZoneHardness(file=file, clean_rounds=1, last_clean=now)
        else:
            self.zones[file].clean_rounds += 1
            self.zones[file].last_clean = now

    def is_hardened(self, file: str, threshold: int = 3) -> bool:
        z = self.zones.get(file)
        return bool(z and z.clean_rounds >= threshold)

    def priority_for_zone(self, file: str) -> float:
        """0.0 (nada prioritario) .. 1.0 (máxima prioridad de escaneo)."""
        z = self.zones.get(file)
        if not z:
            return 0.5  # nuevo, prioridad media
        # Menos rondas limpias = más prioritario.
        if z.clean_rounds >= 5:
            return 0.0
        if z.clean_rounds >= 3:
            return 0.1
        return 0.6

    # ---- Resumen ----
    def summary(self) -> str:
        lines = [
            '=== Memoria del harness ===',
            f'  Rondas totales:        {self.total_rounds}',
            f'  Bugs encontrados:      {self.total_bugs_found}',
            f'  Falsos positivos:      {len(self.false_positives)}',
            f'  Fixes aplicados:       {self.total_fixes_applied}',
            f'  Zonas tracked:         {len(self.zones)}',
            f'  Última sesión:         {time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_session_at)) if self.last_session_at else "nunca"}',
        ]
        if self.last_session_summary:
            lines.append(f'  Resumen última:        {self.last_session_summary}')
        objs = self.open_bugs()
        if objs:
            lines.append('  Bugs abiertos:')
            for b in objs[:5]:
                lines.append(f'    - {b.file}:{b.line}  ({b.times_confirmed}x)  {b.snippet[:80]}')
        return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'summary'
    mem = Memory.load()
    if cmd == 'summary':
        print(mem.summary())
    elif cmd == 'forget':
        # Olvida un bug por hash (debugging).
        if len(sys.argv) < 3:
            print('uso: python memory.py forget <hash>')
            sys.exit(1)
        if sys.argv[2] in mem.bugs:
            del mem.bugs[sys.argv[2]]
            mem.save()
            print(f'Olvidado {sys.argv[2]}')
        else:
            print(f'No existe {sys.argv[2]}')
    else:
        print('uso: summary | forget <hash>')
