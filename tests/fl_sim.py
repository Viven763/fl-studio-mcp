"""A tiny stand-in for FL Studio's scripting modules, to run debug.exec snippets in tests.

Only what the snippets use is modelled: plugin parameters (numeric, dB/Hz and
text-valued ones), mixer routes/names/faders, channel names and targets.
Results go through JSON like the real SysEx transport.
"""

from __future__ import annotations

import json
import math

from fl_studio_mcp.utils.fl_exec import build_code

EQ_TYPES = ["Off", "Low pass", "Band pass", "High pass", "Notch", "Low shelf", "Peaking",
            "High shelf"]


class Param:
    def __init__(self, name, value=0.5, kind="pct", options=None):
        self.name, self.value, self.kind, self.options = name, value, kind, options

    def text(self):
        if self.options:
            last = len(self.options) - 1
            return self.options[min(last, round(self.value * last))]
        if self.kind == "db":
            return f"{-60 + 72 * self.value:.1f}dB"
        if self.kind == "hz":
            hz = 20 * 1000 ** self.value
            return f"{hz / 1000:.1f}kHz" if hz >= 1000 else f"{hz:.0f}Hz"
        return f"{round(self.value * 100)}%"


class Plugins:
    def __init__(self, plugins):
        self.plugins = plugins  # {(index, slot): (name, [Param])}
        self.sets = 0

    def _p(self, i, s):
        return self.plugins[(i, s)]

    def isValid(self, i, s=-1, g=False):  # noqa: N802
        return (i, s) in self.plugins

    def getPluginName(self, i, s=-1, user=False, g=False):  # noqa: N802
        return self._p(i, s)[0]

    def getParamCount(self, i, s=-1, g=False):  # noqa: N802
        return len(self._p(i, s)[1])

    def getParamName(self, p, i, s=-1, g=False):  # noqa: N802
        return self._p(i, s)[1][p].name

    def getParamValue(self, p, i, s=-1, g=False):  # noqa: N802
        return self._p(i, s)[1][p].value

    def setParamValue(self, v, p, i, s=-1, pickup=0, g=False):  # noqa: N802
        self.sets += 1
        self._p(i, s)[1][p].value = max(0.0, min(1.0, v))

    def getParamValueString(self, p, i, s=-1, g=False):  # noqa: N802 - FL 2026: 4 args max
        return self._p(i, s)[1][p].text()


class Mixer:
    def __init__(self, tracks=20):
        self.n = tracks + 1  # plus FL's "current" track
        self.names = ["Master"] + [f"Insert {t}" for t in range(1, self.n)]
        self.routes = {(t, 0): 0.8 for t in range(1, self.n - 1)}
        self.volumes = [0.8] * self.n
        self.after_routing = 0

    def trackCount(self):  # noqa: N802
        return self.n

    def getTrackName(self, t):  # noqa: N802
        return self.names[t]

    def setTrackName(self, t, name):  # noqa: N802
        self.names[t] = name

    def getRouteSendActive(self, s, d):  # noqa: N802
        return (s, d) in self.routes

    def getRouteToLevel(self, s, d):  # noqa: N802
        return self.routes.get((s, d), 0.8)

    def setRouteTo(self, s, d, on, update_ui=False):  # noqa: N802
        if on:
            self.routes.setdefault((s, d), 0.8)
        else:
            self.routes.pop((s, d), None)

    def setRouteToLevel(self, s, d, level):  # noqa: N802
        self.routes[(s, d)] = level

    def afterRoutingChanged(self):  # noqa: N802
        self.after_routing += 1

    def getTrackVolume(self, t, mode=0):  # noqa: N802
        v = self.volumes[t]
        if not mode:
            return v
        return 40 * math.log10(v / 0.8) if v > 0 else float("-inf")

    def setTrackVolume(self, t, v, pickup=0):  # noqa: N802
        self.volumes[t] = max(0.0, min(1.0, v))


class Channels:
    def __init__(self, names, targets=None):
        self.names = list(names)
        self.targets = list(targets or [0] * len(names))

    def channelCount(self, g=False):  # noqa: N802
        return len(self.names)

    def getChannelName(self, i, g=False):  # noqa: N802
        return self.names[i]

    def getTargetFxTrack(self, i, g=False):  # noqa: N802
        return self.targets[i]

    def setTargetFxTrack(self, i, ins, g=False):  # noqa: N802
        self.targets[i] = ins


class Transport:
    recording = False

    def isRecording(self):  # noqa: N802
        return self.recording


class Midi:
    PIM_None = 0


class FLSim:
    def __init__(self, plugins=None, channels=(), tracks=20):
        self.plugins = Plugins(plugins or {})
        self.mixer = Mixer(tracks)
        self.channels = Channels(channels)
        self.transport = Transport()
        self.execs = 0

    def __call__(self, code, *, timeout=10.0, args=None):
        self.execs += 1
        scope = {"plugins": self.plugins, "mixer": self.mixer, "channels": self.channels,
                 "transport": self.transport, "midi": Midi, "result": None}
        exec(build_code(code, args), scope)
        return json.loads(json.dumps(scope["result"]))
