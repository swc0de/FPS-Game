"""GLSL loading with a tiny preprocessor.

Panda3D 1.10 has no portable ``#include`` for GLSL, so shaders are
assembled here:

* ``#include "file.glsl"`` is replaced by that file (relative to
  render/shaders), recursively, each file included at most once.
* Quality switches from the graphics preset are injected as ``#define``
  lines right after ``#version``, so a single source compiles into Low..Ultra
  variants (the compiler strips disabled code paths entirely).
"""
from __future__ import annotations

import re

from panda3d.core import Shader

from engine import paths

_INCLUDE = re.compile(r'^\s*#include\s+"([^"]+)"\s*$', re.MULTILINE)
_cache: dict[tuple, Shader] = {}


def _resolve(name: str, seen: set) -> str:
    if name in seen:
        return ""
    seen.add(name)
    text = (paths.SHADER_DIR / name).read_text(encoding="utf-8")

    def repl(m):
        return f"// ---- begin {m.group(1)}\n{_resolve(m.group(1), seen)}\n// ---- end {m.group(1)}"
    return _INCLUDE.sub(repl, text)


def preprocess(name: str, defines: dict | None = None) -> str:
    src = _resolve(name, set())
    lines = src.split("\n")
    out = []
    injected = False
    for line in lines:
        out.append(line)
        if not injected and line.strip().startswith("#version"):
            for key, value in (defines or {}).items():
                if value is True:
                    out.append(f"#define {key} 1")
                elif value is False or value is None:
                    continue
                else:
                    out.append(f"#define {key} {value}")
            injected = True
    return "\n".join(out)


def load(vertex: str, fragment: str, defines: dict | None = None) -> Shader:
    key = (vertex, fragment, tuple(sorted((defines or {}).items())))
    shader = _cache.get(key)
    if shader is None:
        shader = Shader.make(Shader.SL_GLSL, preprocess(vertex, defines), preprocess(fragment, defines))
        if shader is None:
            raise RuntimeError(f"failed to compile shader {vertex} / {fragment}")
        _cache[key] = shader
    return shader


def clear_cache() -> None:
    _cache.clear()
