"""Game state, command interpreter (REPL), waterfall data and scoring."""
from __future__ import annotations

import curses
import json
import math
import os
import random
import time
from collections import deque

from .world import (CLASS_ALIASES, CLASS_NAMES, MAP, World, angdiff, clamp)

BINS = 120
SCORE_FILE = os.path.expanduser("~/.subzero_scores.json")
RANGES = (2, 4, 8, 16, 32)
TIMES = (1, 2, 4, 8, 16, 32)
DEFAULT_LOADOUT = {"hull": 100.0, "battery": 85.0, "torps": 6, "decoys": 4}

HELP = [
    "COMMANDS (case-insensitive, HELM prefix optional):",
    "  HELM HDG 140 SPD 5 DEPTH 300   course / knots / metres in one order",
    "  LEFT 30 · RIGHT 90 · ALL STOP · FLANK · PD (periscope depth 18 m)",
    "  SONAR PASSIVE BROAD | SONAR PASSIVE NARROW 045 | SONAR PING (or PING)",
    "  TRACK S3 (LOFAR + record signature) · BTR (bearing/time display)",
    "  CLASSIFY S3 MERCHANT|TRAWLER|BIO|WARSHIP|SUB|UNKNOWN",
    "  TUBE 1 LOAD TORP|DECOY · TUBE 1 UNLOAD · TUBE 1 FIRE [S3|brg]",
    "  SILENT RUN ON|OFF · SCRUB ON|OFF · SNORKEL ON|OFF (at 18 m)",
    "  RANGE 2|4|8|16|32 · TIME 1..32 · LIB · MISSION · HELP · QUIT",
    "KEYS: Enter run · ↑↓ history · Tab next contact · PgUp/PgDn log",
    "      [ ] time compression · < > scope range · F2 ping · F3 BTR/LOFAR",
]

LIB = [
    "ACOUSTIC SIGNATURE LIBRARY (LOFAR 0-600 Hz):",
    "  MERCHANT  diesel firing line 45-70 Hz + evenly spaced ladder of",
    "            4-5 harmonics, heavy broadband smear. Loud, steady course.",
    "  TRAWLER   generator line 100 or 120 Hz + 2nd harmonic; winch line",
    "            300-450 Hz that comes and goes. Slow, wandering.",
    "  BIOLOGIC  no stable lines. Sweeping song tonal 40-300 Hz, broadband",
    "            click bursts, long silences.",
    "  WARSHIP   gas turbine line 320-380 Hz (strong), reduction gear",
    "            170-200 Hz, blade rate 25-35 Hz + harmonic. Surface ship.",
    "  SUBMARINE very faint 50 or 60 Hz electrical line + 2nd harmonic,",
    "            pump line 230-270 Hz, almost no broadband. Quiet. Deadly.",
    "  TORPEDO   screaming high-speed screws, ~190 & 380-430 Hz.",
]


def fmt_t(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def load_scores():
    try:
        with open(SCORE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_score(entry):
    scores = load_scores()
    scores.append(entry)
    scores.sort(key=lambda s: s.get("score", 0), reverse=True)
    scores = scores[:10]
    try:
        with open(SCORE_FILE, "w") as f:
            json.dump(scores, f, indent=1)
    except OSError:
        pass
    return scores


class Game:
    def __init__(self, seed=None, sound=None):
        self.master_seed = seed if seed is not None else random.randrange(1 << 31)
        self.rng = random.Random(self.master_seed)
        self.fx = random.Random()
        self.sound = sound
        self.quit = False
        self.real = 0.0
        self.world: World | None = None
        self.new_run()

    # ------------------------------------------------------------ lifecycle
    def new_run(self):
        self.run_score = 0
        self.run_items: list[tuple[str, int]] = []
        self.patrol = 1
        self.loadout = dict(DEFAULT_LOADOUT)
        self.scores = load_scores()
        self.result = None
        self.start_patrol()

    def start_patrol(self):
        self.logs: list[tuple[str, str, str]] = []
        self.world = None
        self.input = ""
        self.history: list[str] = getattr(self, "history", [])
        self.hidx = None
        self.log_scroll = 0
        self.tc = 1
        self.range = 8
        self.wf = "BTR"
        self.btr = deque(maxlen=200)
        self.lofar = deque(maxlen=200)
        self.wf_acc = 0.0
        self.ping_real = -99.0
        self.enemy_pings: list[tuple[float, float]] = []
        self.flash_until = -1.0
        self.shake_until = -1.0
        self.world = World(self.rng.randrange(1 << 31), self.patrol, self.loadout, self.log)
        self.mode = "brief"
        w = self.world
        self.log(f"Patrol {self.patrol} begins. Depth {w.player.depth:.0f} m, layer reported at "
                 f"{w.env.layer_depth:.0f} m.", "c")
        self.log("Type HELP for orders, LIB for the signature library. Good hunting, Captain.", "c")

    def log(self, text, lvl="i"):
        stamp = fmt_t(self.world.t) if self.world else "00:00:00"
        self.logs.append((stamp, text, lvl))
        if len(self.logs) > 400:
            del self.logs[:100]
        if self.log_scroll:
            self.log_scroll += 1

    # ---------------------------------------------------------------- tick
    def update(self, dt):
        self.real += dt
        if self.mode != "play":
            return
        w = self.world
        w.step(dt * self.tc)
        self._events()
        self.wf_acc += dt
        if self.wf_acc >= 0.3:
            self.wf_acc = 0.0
            self.btr.appendleft(self.btr_row())
            self.lofar.appendleft(self.lofar_row())
        self.enemy_pings = [(b, t) for b, t in self.enemy_pings if self.real - t < 2.0]
        if w.over:
            self._finish(*w.over)

    def _drop_tc(self):
        if self.tc > 1:
            self.tc = 1
            self.log("Time compression OFF.", "d")

    def _events(self):
        w = self.world
        snd = self.sound
        play = snd.play if snd else (lambda *a, **k: None)
        for ev in w.events:
            k = ev[0]
            if k == "ping":
                self.ping_real = self.real
                play("ping")
            elif k == "enemy_ping":
                self.enemy_pings.append((ev[1], self.real))
                play("ping", 0.3)
                self._drop_tc()
            elif k == "contact":
                play("blip")
                if self.tc > 4:
                    self._drop_tc()
            elif k in ("torpedo", "alarm"):
                play("alarm", 1.5)
                self.flash_until = self.real + 1.0
                self._drop_tc()
            elif k == "hit":
                play("boom")
                self.flash_until = self.real + 1.5
                self.shake_until = self.real + 1.5
                self._drop_tc()
            elif k == "explosion":
                play("boom", 0.3)
                if ev[1] < 1500:
                    self.shake_until = self.real + 0.8
            elif k == "creak":
                play("creak", 3.0)
                self.shake_until = self.real + 0.5
            elif k in ("intel", "launch"):
                play("blip")
        w.events.clear()

    def _finish(self, kind, reason):
        w = self.world
        items = list(w.score_items)
        for tr in w.tracks.values():
            if tr.cls and not tr.is_torp:
                ok = tr.cls == tr.ent.kind
                items.append((f"Classified {tr.sid} as {CLASS_NAMES[tr.cls]}", 100 if ok else -75))
        rec = sum(1 for tr in w.tracks.values() if tr.recorded and tr.ent.kind in ("warship", "sub"))
        met = rec >= w.need
        if kind == "exfil":
            items.append((f"Exfiltration, patrol {self.patrol}", 500 * self.patrol))
            if met:
                items.append(("Mission objectives met", 300 * self.patrol))
        pts = sum(p for _, p in items)
        self.run_score += pts
        self.run_items.extend(items)
        self.result = {"kind": kind, "reason": reason, "items": items, "patrol_pts": pts,
                       "met": met, "rec": rec, "need": w.need, "time": w.t}
        if kind == "exfil" and met:
            p = w.player
            self.loadout = {"hull": min(100.0, p.hull + 50), "battery": 85.0,
                            "torps": 6, "decoys": 4}
            self.mode = "debrief"
        else:
            if kind == "exfil":
                self.result["reason"] = ("Reached exfil without the required signatures. "
                                         "Fleet relieves you of command.")
            self.mode = "over"
            self.scores = save_score({"score": self.run_score, "patrol": self.patrol,
                                      "cause": self.result["reason"][:60],
                                      "date": time.strftime("%Y-%m-%d")})
        if self.sound:
            self.sound.play("boom" if kind == "dead" else "blip")

    # ------------------------------------------------------------ waterfall
    def btr_row(self):
        w, p, fx = self.world, self.world.player, self.fx
        base = 0.10 + clamp((p.self_noise() - 50) / 60, 0, 0.3)
        row = [fx.random() * base for _ in range(BINS)]
        for e in w.entities:
            if e is p or not e.alive or e.side == "own" or e.se_own < -12:
                continue
            v = clamp((e.se_own + 12) / 26, 0, 1)
            b = int(e.brg_own / 3) % BINS
            row[b] += v
            if v > 0.5:
                row[(b - 1) % BINS] += v * 0.35
                row[(b + 1) % BINS] += v * 0.35
        for brg, t in self.enemy_pings:
            if self.real - t < 0.6:
                row = [v + 0.2 for v in row]
                row[int(brg / 3) % BINS] = 1.5
        if w.t < w.deafen_until:
            row = [v + 0.6 * fx.random() for v in row]
        for i in range(BINS):
            rel = angdiff(i * 3 + 1.5, p.heading)
            if abs(rel) > 150:
                row[i] *= 0.15
            elif p.sonar == "NARROW" and abs(angdiff(i * 3 + 1.5, p.narrow_brg)) > 20:
                row[i] *= 0.5
        return row

    def lofar_row(self):
        w, fx = self.world, self.fx
        row = [fx.random() * 0.15 for _ in range(BINS)]
        tr = w.selected
        if tr is None or not tr.ent.alive:
            return row
        e = tr.ent
        s = clamp((e.se_own + 6) / 18, 0, 1) * (0.15 if e.baffled else 1.0)
        lines, bb = e.lines(w.t)
        for f, a in lines:
            b = int(f / 5)
            if 0 <= b < BINS:
                row[b] += s * a * (0.6 + 0.4 * fx.random())
        for i in range(BINS):
            row[i] += s * bb * fx.random() * 0.6
        return row

    # --------------------------------------------------------------- input
    def handle_key(self, ch):
        if self.mode == "brief":
            if ch in (10, 13, curses.KEY_ENTER, 32):
                self.mode = "play"
            elif ch in (ord("q"), ord("Q"), 27):
                self.quit = True
            return
        if self.mode == "debrief":
            if ch in (10, 13, curses.KEY_ENTER, 32):
                self.patrol += 1
                self.start_patrol()
            return
        if self.mode == "over":
            if ch in (ord("r"), ord("R")):
                self.new_run()
            elif ch in (ord("q"), ord("Q"), 27):
                self.quit = True
            return
        if ch in (10, 13, curses.KEY_ENTER):
            line = self.input.strip()
            self.input = ""
            self.hidx = None
            if line:
                if not self.history or self.history[-1] != line:
                    self.history.append(line)
                self.log_scroll = 0
                self.execute(line)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.input = self.input[:-1]
        elif ch == 27:
            self.input = ""
        elif ch == curses.KEY_UP and self.history:
            self.hidx = len(self.history) - 1 if self.hidx is None else max(0, self.hidx - 1)
            self.input = self.history[self.hidx]
        elif ch == curses.KEY_DOWN and self.hidx is not None:
            self.hidx += 1
            if self.hidx >= len(self.history):
                self.hidx, self.input = None, ""
            else:
                self.input = self.history[self.hidx]
        elif ch == 9:
            self.cycle_track()
        elif ch == curses.KEY_PPAGE:
            self.log_scroll = min(len(self.logs), self.log_scroll + 5)
        elif ch == curses.KEY_NPAGE:
            self.log_scroll = max(0, self.log_scroll - 5)
        elif ch == curses.KEY_F1:
            self.execute("HELP")
        elif ch == curses.KEY_F2:
            self.execute("PING")
        elif ch == curses.KEY_F3:
            self.wf = "LOFAR" if self.wf == "BTR" else "BTR"
        elif ch == ord("["):
            self.set_tc(TIMES[max(0, TIMES.index(self.tc) - 1)])
        elif ch == ord("]"):
            self.set_tc(TIMES[min(len(TIMES) - 1, TIMES.index(self.tc) + 1)])
        elif ch == ord("<"):
            self.range = RANGES[max(0, RANGES.index(self.range) - 1)]
        elif ch == ord(">"):
            self.range = RANGES[min(len(RANGES) - 1, RANGES.index(self.range) + 1)]
        elif 32 <= ch < 127 and len(self.input) < 60:
            self.input += chr(ch)

    def set_tc(self, n):
        self.tc = n
        self.log(f"Time compression x{n}.", "d")

    def sorted_tracks(self):
        w = self.world
        trs = [tr for tr in w.tracks.values()
               if tr.ent.alive or w.t - tr.last < 120]
        trs.sort(key=lambda tr: (not (tr.is_torp and tr.held), tr.lost, int(tr.sid[1:]) if tr.sid[1:].isdigit() else 0))
        return trs

    def cycle_track(self):
        w = self.world
        trs = [tr for tr in self.sorted_tracks() if not tr.is_torp and tr.ent.alive]
        if not trs:
            return
        i = trs.index(w.selected) + 1 if w.selected in trs else 0
        self.select(trs[i % len(trs)])

    def select(self, tr):
        self.world.selected = tr
        self.wf = "LOFAR"
        self.lofar.clear()
        self.log(f"SONAR: narrowband analysis on {tr.sid}, recording signature.", "g")

    # ------------------------------------------------------------ commands
    def execute(self, line):
        toks = line.upper().replace(",", " ").split()
        self.log("> " + " ".join(toks), "w")
        c, a = toks[0], toks[1:]
        try:
            fn = self.COMMANDS.get(c)
            if fn is None:
                if c in self.HELM_WORDS:
                    return self.cmd_helm(toks)
                return self.log(f"Unknown order '{c}'. Type HELP.", "y")
            fn(self, a)
        except (ValueError, IndexError):
            self.log("Unable to comply — check the order's syntax (HELP).", "y")

    HELM_WORDS = {"HDG", "H", "COURSE", "SPD", "SPEED", "DEPTH", "DEP", "D", "LEFT", "L", "RIGHT",
                  "R", "STOP", "ALL", "FLANK", "PD", "PERISCOPE"}

    def cmd_helm(self, a):
        w = self.world
        p = w.player
        i, said = 0, []
        while i < len(a):
            k = a[i]
            if k in ("HDG", "H", "COURSE"):
                p.ord_hdg = float(a[i + 1]) % 360
                said.append(f"come to {p.ord_hdg:03.0f}")
                i += 2
            elif k in ("LEFT", "L", "RIGHT", "R"):
                n = float(a[i + 1])
                p.ord_hdg = (p.heading + (-n if k[0] == "L" else n)) % 360
                said.append(f"{'left' if k[0] == 'L' else 'right'} {n:.0f}, new course {p.ord_hdg:03.0f}")
                i += 2
            elif k in ("SPD", "SPEED", "S"):
                v = float(a[i + 1])
                if not 0 <= v <= p.max_speed:
                    return self.log(f"HELM: speed must be 0-{p.max_speed:.0f} kt.", "y")
                p.ord_spd = v
                said.append(f"make turns for {v:.0f} knots")
                i += 2
            elif k == "FLANK":
                p.ord_spd = p.max_speed
                said.append("ahead flank")
                i += 1
            elif k in ("STOP",):
                p.ord_spd = 0
                said.append("all stop")
                i += 1
            elif k in ("DEPTH", "DEP", "D", "PD", "PERISCOPE"):
                if k in ("PD", "PERISCOPE"):
                    v, i = 18.0, i + 1
                else:
                    v, i = float(a[i + 1]), i + 2
                if v < 10:
                    return self.log("DIVING OFFICER: minimum depth is 10 m — we do not surface out here.", "y")
                p.ord_depth = v
                said.append(f"make depth {v:.0f} m")
                if v > p.crush_depth:
                    self.log(f"DIVING OFFICER: Captain, {v:.0f} m is past CRUSH DEPTH "
                             f"({p.crush_depth:.0f} m)!", "R")
                elif v > p.test_depth:
                    self.log(f"DIVING OFFICER: {v:.0f} m is below test depth ({p.test_depth:.0f} m). "
                             f"She won't like it.", "y")
                if v > w.floor_here - 10:
                    self.log(f"NAVIGATOR: bottom is at {w.floor_here:.0f} m here!", "y")
            elif k in ("ALL", "HELM"):
                i += 1
            else:
                return self.log(f"HELM: don't understand '{k}'.", "y")
        if said:
            s = ", ".join(said)
            self.log(f"HELM: {s[0].upper() + s[1:]}, aye.", "g")
        if p.silent and p.ord_spd > 6:
            self.log("CHIEF: speed over 6 kt will be heard despite silent routine.", "y")
        if p.ord_spd > 0 and p.battery <= 0:
            self.log("CHIEF: no battery — the motor is dead.", "R")

    def cmd_sonar(self, a):
        p = self.world.player
        if not a:
            return self.log(f"SONAR: passive {p.sonar}.", "g")
        if a[0] in ("PING", "ACTIVE"):
            return self.cmd_ping([])
        if a[0] == "PASSIVE":
            a = a[1:]
        if not a or a[0] == "BROAD":
            p.sonar = "BROAD"
            return self.log("SONAR: passive broadband, all-round search.", "g")
        if a[0] == "NARROW":
            p.narrow_brg = float(a[1]) % 360 if len(a) > 1 else p.heading
            p.sonar = "NARROW"
            return self.log(f"SONAR: narrow beam steered to {p.narrow_brg:03.0f} (+6 dB in ±20°, "
                            f"deaf elsewhere).", "g")
        self.log("SONAR: PASSIVE BROAD | PASSIVE NARROW <brg> | PING", "y")

    def cmd_ping(self, a):
        w = self.world
        if w.player.battery < 1:
            return self.log("SONAR: not enough battery to transmit.", "y")
        n = w.player_ping()
        self.log(f"SONAR: PING! Active transmission — {n} echo{'es' if n != 1 else ''}. "
                 f"Every ear in the ocean heard that.", "y")

    def cmd_tube(self, a):
        w = self.world
        p = w.player
        n = int(a[0])
        if not 1 <= n <= 4:
            return self.log("Tubes are numbered 1-4.", "y")
        op = a[1] if len(a) > 1 else "FIRE"
        tb = p.tubes[n - 1]
        if op == "LOAD":
            what = {"TORP": "TORP", "TORPEDO": "TORP", "T": "TORP", "DECOY": "DECOY", "D": "DECOY",
                    "NOISEMAKER": "DECOY"}.get(a[2] if len(a) > 2 else "TORP")
            if what is None:
                return self.log("Load TORP or DECOY.", "y")
            if tb["load"] is not None:
                return self.log(f"Tube {n} already holds a {tb['load']}. UNLOAD it first.", "y")
            store = "torps" if what == "TORP" else "decoys"
            if getattr(p, store) <= 0:
                return self.log(f"TORPEDO ROOM: no {what}s left in the racks.", "y")
            setattr(p, store, getattr(p, store) - 1)
            tb["load"], tb["timer"] = what, 45.0 if what == "TORP" else 25.0
            return self.log(f"TORPEDO ROOM: loading tube {n} with {what}, {tb['timer']:.0f} s.", "g")
        if op == "UNLOAD":
            if tb["load"] is None:
                return self.log(f"Tube {n} is already empty.", "y")
            store = "torps" if tb["load"] == "TORP" else "decoys"
            setattr(p, store, getattr(p, store) + 1)
            tb["load"], tb["timer"] = None, 0.0
            return self.log(f"TORPEDO ROOM: tube {n} unloaded.", "g")
        if op == "FIRE":
            msg, lvl = w.fire_tube(n, a[2] if len(a) > 2 else None)
            return self.log(msg, lvl)
        self.log("TUBE n LOAD TORP|DECOY · TUBE n UNLOAD · TUBE n FIRE [Sx|brg]", "y")

    def cmd_fire(self, a):
        self.cmd_tube([a[0], "FIRE"] + a[1:2])

    def cmd_load(self, a):
        self.cmd_tube([a[0], "LOAD"] + a[1:2])

    @staticmethod
    def _onoff(a, cur):
        if not a:
            return not cur
        if a[-1] in ("ON", "1", "YES"):
            return True
        if a[-1] in ("OFF", "0", "NO"):
            return False
        raise ValueError

    def cmd_silent(self, a):
        p = self.world.player
        p.silent = self._onoff([x for x in a if x != "RUN"], p.silent)
        if p.silent:
            p.scrub = False
            self.log("XO: Rig for silent running. Non-essential machinery off, scrubbers secured, "
                     "crew to soft shoes.", "g")
            if p.ord_spd > 6:
                self.log("XO: recommend slowing to 6 kt or less.", "y")
        else:
            self.log("XO: Secure from silent running.", "g")

    def cmd_scrub(self, a):
        p = self.world.player
        on = self._onoff(a, p.scrub)
        if on and p.battery <= 0:
            return self.log("CHIEF: no power for the scrubbers.", "y")
        p.scrub = on
        self.log(f"CHIEF: CO2 scrubbers {'running (+2 dB noise)' if on else 'secured — watch the air'}.", "g")

    def cmd_snorkel(self, a):
        p = self.world.player
        p.snorkel = self._onoff(a, p.snorkel)
        if p.snorkel:
            self.log("CHIEF: Raise the snorkel, start the diesels. We'll be LOUD"
                     + ("." if p.depth <= 20.5 else " — once we're at 18 m (PD)."), "y")
        else:
            self.log("CHIEF: Diesels stopped, snorkel lowered.", "g")

    def cmd_classify(self, a):
        w = self.world
        tr = w.find_track(a[0])
        if tr is None:
            return self.log(f"No contact {a[0]}.", "y")
        if tr.is_torp:
            return self.log(f"{tr.sid} is a torpedo. There's no doubt about that.", "y")
        if a[1] not in CLASS_ALIASES:
            return self.log("Classes: MERCHANT TRAWLER BIO WARSHIP SUB UNKNOWN", "y")
        tr.cls = CLASS_ALIASES[a[1]]
        name = CLASS_NAMES[tr.cls] if tr.cls else "UNKNOWN"
        self.log(f"SONAR: {tr.sid} classified {name}.", "g")

    def cmd_track(self, a):
        w = self.world
        if not a or a[0] in ("OFF", "NONE"):
            w.selected = None
            self.wf = "BTR"
            return self.log("SONAR: narrowband analysis stopped.", "g")
        tr = w.find_track(a[0])
        if tr is None:
            return self.log(f"No contact {a[0]}.", "y")
        self.select(tr)

    def cmd_wf(self, a):
        if a and a[0] in ("LOFAR", "NB"):
            self.wf = "LOFAR"
        else:
            self.wf = "BTR"

    def cmd_btr(self, a):
        self.wf = "BTR"

    def cmd_range(self, a):
        if a and a[0] == "RANGE":
            a = a[1:]
        v = int(float(a[0]))
        if v not in RANGES:
            return self.log(f"Scope ranges: {', '.join(map(str, RANGES))} nm.", "y")
        self.range = v

    def cmd_time(self, a):
        v = int(float(a[0]))
        if v not in TIMES:
            return self.log(f"Time compression: {', '.join(map(str, TIMES))}.", "y")
        self.set_tc(v)

    def cmd_help(self, a):
        for ln in HELP:
            self.log(ln, "c")

    def cmd_lib(self, a):
        for ln in LIB:
            self.log(ln, "c")

    def cmd_mission(self, a):
        w = self.world
        p = w.player
        rec = sum(1 for tr in w.tracks.values() if tr.recorded and tr.ent.kind in ("warship", "sub"))
        d = math.hypot(w.exfil[0] - p.x, w.exfil[1] - p.y)
        self.log(f"ORDERS: record acoustic signatures of {w.need} hostile military unit(s) "
                 f"(warship or submarine) — {rec}/{w.need} done.", "c")
        self.log(f"ORDERS: then proceed to exfil point X, {d:.1f} nm away. Sinking neutrals "
                 f"is a court-martial offence.", "c")

    def cmd_quit(self, a):
        w = self.world
        w.over = ("dead", "Patrol abandoned by the captain.")
        self._finish(*w.over)

    COMMANDS = {
        "HELM": cmd_helm, "SONAR": cmd_sonar, "PING": cmd_ping, "TUBE": cmd_tube, "FIRE": cmd_fire,
        "LOAD": cmd_load, "SILENT": cmd_silent, "SCRUB": cmd_scrub, "SCRUBBERS": cmd_scrub,
        "SNORKEL": cmd_snorkel, "CLASSIFY": cmd_classify, "CLASS": cmd_classify, "CL": cmd_classify,
        "TRACK": cmd_track, "LOFAR": cmd_track, "SEL": cmd_track, "WF": cmd_wf, "BTR": cmd_btr,
        "RANGE": cmd_range, "SCOPE": cmd_range, "TIME": cmd_time, "TC": cmd_time, "HELP": cmd_help,
        "?": cmd_help, "LIB": cmd_lib, "LIBRARY": cmd_lib, "MISSION": cmd_mission,
        "ORDERS": cmd_mission, "QUIT": cmd_quit, "EXIT": cmd_quit,
    }


__all__ = ["Game", "fmt_t", "BINS", "MAP"]
