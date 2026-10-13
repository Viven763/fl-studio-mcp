"""Plugin parameters by index, name, displayed number or displayed text.

Works for channel instruments (slot -1, global channel index) and mixer
effects (index = insert, slot 0-9). Everything runs inside FL via debug.exec
with a small helper library: parameter names are fetched in chunks (ZENOLOGY
exposes thousands) and cached; text-valued parameters (e.g. EQ band type
"High pass") are set by scanning the normalized range for the label, always
restoring the original value if the label isn't found.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fl_studio_mcp.utils.fl_exec import exec_in_fl

# Runs inside FL (prepended to every snippet below).
FL_PARAM_LIB = r'''
import re as _re
try:
    import time as _time
    _clock = _time.time
except Exception:
    _clock = None
def _gv(p, i, s):
    return plugins.getParamValue(p, i, s, True)
def _sv(p, i, s, v):
    plugins.setParamValue(v, p, i, s, midi.PIM_None, True)
def _pv(p, i, s):
    return plugins.getParamValueString(p, i, s, midi.PIM_None, True)
def _num(t):
    m = _re.search(r"-?\d+(?:[.,]\d+)?", t or "")
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    return v * 1000 if "kHz" in (t or "") else v
def _norm(t):
    return _re.sub(r"[^a-z0-9]", "", (t or "").lower())
def _over(t0, budget, n, max_n):
    return (_clock() - t0 > budget) if _clock else n >= max_n
def bisect(p, i, s, target):
    def at(v):
        _sv(p, i, s, v)
        return _num(_pv(p, i, s))
    lo, hi = 0.0, 1.0
    rising = (at(1.0) or 0) > (at(0.0) or 0)
    for _ in range(30):
        mid = (lo + hi) / 2
        x = at(mid)
        if x is None or (x < target) == rising:
            lo = mid
        else:
            hi = mid
    def miss(v):
        x = at(v)
        return abs(x - target) if x is not None else 1e18
    _sv(p, i, s, min((lo, hi), key=miss))
    return _pv(p, i, s)
def scan(p, i, s, steps=128, budget=3.0):
    if transport.isRecording():
        raise RuntimeError("refusing to scan parameter values while recording")
    old = _gv(p, i, s)
    t0 = _clock() if _clock else 0
    out, partial = [], False
    try:
        for k in range(steps + 1):
            if _over(t0, budget, k, steps + 1):
                partial = True
                break
            v = k / steps
            _sv(p, i, s, v)
            label = _pv(p, i, s)
            if out and out[-1][0] == label:
                out[-1][2] = v
            else:
                out.append([label, v, v])
    finally:
        _sv(p, i, s, old)
    return out, partial
def pick(opts, want):
    w = _norm(want)
    exact = [o for o in opts if _norm(o[0]) == w]
    if exact:
        return exact[0]
    for test in (lambda t: t.startswith(w), lambda t: w in t):
        hits = [o for o in opts if test(_norm(o[0]))]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    return None
def set_text(p, i, s, want):
    opts, partial = scan(p, i, s)
    o = pick(opts, want)
    if o is None:
        return {"error": "no single option matches %r" % want,
                "options": [x[0] for x in opts][:64], "partial": partial}
    _sv(p, i, s, (o[1] + o[2]) / 2)
    return {"text": _pv(p, i, s), "value": round(_gv(p, i, s), 4)}
def names_chunk(i, s, start, max_bytes=9000, budget=0.4):
    n = plugins.getParamCount(i, s, True)
    t0 = _clock() if _clock else 0
    names, size, k = [], 0, start
    while k < n:
        name = plugins.getParamName(k, i, s, True)
        names.append(name)
        size += len(name) + 4
        k += 1
        if size > max_bytes or _over(t0, budget, k - start, 400):
            break
    return {"n": n, "next": k if k < n else None, "names": names}
def describe(p, i, s):
    return {"index": p, "name": plugins.getParamName(p, i, s, True),
            "value": round(_gv(p, i, s), 4), "text": _pv(p, i, s)}
'''

# Parameters that act when moved (randomize, reset...) are never scanned in bulk.
RISKY = re.compile(r"random|reset|init|panic|learn|trigger|preset|program|bank|patch", re.I)
_NAME_CACHE: dict[tuple, list[str]] = {}

ExecFn = Callable[..., Any]


def _run(body: str, args: dict, exec_fn: ExecFn | None, timeout: float = 10.0) -> Any:
    fn = exec_fn or exec_in_fl
    return fn(FL_PARAM_LIB + body, timeout=timeout, args=args)


def plugin_info(index: int, slot: int = -1, exec_fn: ExecFn | None = None) -> dict:
    """{"name", "count"} of the plugin at channel `index` (slot -1) or insert/slot."""
    info = _run(
        "result = {'valid': plugins.isValid(ARGS['i'], ARGS['s'], True)}\n"
        "if result['valid']:\n"
        "    result['name'] = plugins.getPluginName(ARGS['i'], ARGS['s'], False, True)\n"
        "    result['count'] = plugins.getParamCount(ARGS['i'], ARGS['s'], True)",
        {"i": index, "s": slot}, exec_fn,
    )
    if not info.get("valid"):
        where = f"channel {index}" if slot < 0 else f"insert {index} slot {slot}"
        raise ValueError(f"no plugin at {where}")
    return info


def param_names(index: int, slot: int = -1, exec_fn: ExecFn | None = None) -> tuple[str, list[str]]:
    """(plugin name, all parameter names), fetched in chunks and cached."""
    info = plugin_info(index, slot, exec_fn)
    key = (index, slot, info["name"], info["count"])
    if key not in _NAME_CACHE:
        names: list[str] = []
        start: int | None = 0
        while start is not None:
            chunk = _run(
                "result = names_chunk(ARGS['i'], ARGS['s'], ARGS['start'])",
                {"i": index, "s": slot, "start": start}, exec_fn,
            )
            names += chunk["names"]
            start = chunk["next"]
        _NAME_CACHE[key] = names
    return info["name"], _NAME_CACHE[key]


def search_params(names: list[str], query: str, limit: int = 20) -> list[int]:
    """Indices of named parameters containing every word of `query` (case-insensitive)."""
    words = query.lower().split()
    return [i for i, n in enumerate(names) if n and all(w in n.lower() for w in words)][:limit]


def resolve_param(names: list[str], key: int | str) -> int:
    """A parameter index from an index or an exact (case-insensitive) name."""
    if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
        idx = int(key)
        if not 0 <= idx < len(names):
            raise ValueError(f"parameter index {idx} out of range (0-{len(names) - 1})")
        return idx
    want = str(key).strip().lower()
    exact = [i for i, n in enumerate(names) if n.strip().lower() == want]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ValueError(f"parameter name {key!r} is ambiguous: indices {exact[:10]}")
    similar = [f"{i}: {names[i]}" for i in search_params(names, str(key), 6)]
    raise ValueError(f"no parameter named {key!r}" + (f"; similar: {similar}" if similar else ""))


def describe_params(index: int, slot: int, params: list[int],
                    exec_fn: ExecFn | None = None) -> list[dict]:
    return _run(
        "result = [describe(p, ARGS['i'], ARGS['s']) for p in ARGS['ps']]",
        {"i": index, "s": slot, "ps": params}, exec_fn,
    )


def param_options(index: int, slot: int, param: int,
                  exec_fn: ExecFn | None = None) -> dict:
    """Labels of a text-valued parameter with their normalized ranges."""
    res = _run(
        "o, part = scan(ARGS['p'], ARGS['i'], ARGS['s'])\nresult = {'options': o, 'partial': part}",
        {"i": index, "s": slot, "p": param}, exec_fn, timeout=8.0,
    )
    return {
        "options": [label for label, _lo, _hi in res["options"]],
        "ranges": res["options"],
        "partial": res["partial"],
    }


def is_textual(text: str) -> bool:
    return bool(text) and not re.search(r"\d", text)


def set_params(index: int, slot: int, items: list[dict],
               exec_fn: ExecFn | None = None) -> dict:
    """Set parameters; each item = {"param": index|name} + one of value / target / text."""
    names: list[str] | None = None
    plan: list[tuple[int, str, Any]] = []
    errors: list[str] = []
    for n, item in enumerate(items):
        kinds = [k for k in ("value", "target", "text") if k in item]
        if "param" not in item or len(kinds) != 1:
            errors.append(f"item #{n}: give 'param' and exactly one of value/target/text")
            continue
        key = item["param"]
        try:
            if isinstance(key, int):
                idx = key
            else:
                if names is None:
                    names = param_names(index, slot, exec_fn)[1]
                idx = resolve_param(names, key)
        except ValueError as e:
            errors.append(f"item #{n}: {e}")
            continue
        plan.append((idx, kinds[0], item[kinds[0]]))

    out: dict[str, dict] = {}
    numeric = [(p, k, v) for p, k, v in plan if k != "text"]
    if numeric:
        body = ["done = []"]
        body.append(
            "for p, k, v in ARGS['plan']:\n"
            "    if k == 'value':\n"
            "        _sv(p, ARGS['i'], ARGS['s'], float(v))\n"
            "    else:\n"
            "        bisect(p, ARGS['i'], ARGS['s'], float(v))\n"
            "    done.append(describe(p, ARGS['i'], ARGS['s']))\n"
            "result = done"
        )
        for d in _run("\n".join(body), {"i": index, "s": slot, "plan": numeric}, exec_fn,
                      timeout=30.0):
            out[str(d["index"])] = {"name": d["name"], "text": d["text"], "value": d["value"]}
    for p, _k, want in [x for x in plan if x[1] == "text"]:
        res = _run("result = set_text(ARGS['p'], ARGS['i'], ARGS['s'], ARGS['want'])",
                   {"i": index, "s": slot, "p": p, "want": str(want)}, exec_fn, timeout=10.0)
        if "error" in res:
            errors.append(f"param {p}: {res['error']}; options: {res['options']}")
        else:
            d = describe_params(index, slot, [p], exec_fn)[0]
            out[str(p)] = {"name": d["name"], "text": res["text"], "value": res["value"]}
    return {"params": out, "errors": errors}
