"""Curses renderer: PPI scope, waterfall, telemetry, depth profile and console."""
from __future__ import annotations

import curses
import math
import textwrap

from .game import BINS, fmt_t
from .world import CLASS_NAMES, MAP, angdiff, bearing, clamp

RAMP = " .:-=+*#%@"
SWEEP_PERIOD = 4.0
SYMS = {None: "?", "merchant": "#", "trawler": "#", "whale": "~", "warship": "!", "sub": "!",
        "torpedo": "*", "decoy": "o"}
TITLE = [
    "  ____        _     _____               ",
    " / ___| _   _| |__ |__  /___ _ __ ___   ",
    " \\___ \\| | | | '_ \\  / // _ \\ '__/ _ \\  ",
    "  ___) | |_| | |_) |/ /|  __/ | | (_) | ",
    " |____/ \\__,_|_.__//____\\___|_|  \\___/  ",
]


class UI:
    def __init__(self, scr):
        self.scr = scr
        self.blips: dict[str, dict] = {}
        self.trails: dict[str, list] = {}
        self.prev_sweep = 0.0
        self.last_now = None
        self.terrain_key = None
        self.terrain_cells: list = []
        self._init_colors()

    def _init_colors(self):
        A = {}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            pairs = {"g": curses.COLOR_GREEN, "y": curses.COLOR_YELLOW, "r": curses.COLOR_RED,
                     "c": curses.COLOR_CYAN, "w": curses.COLOR_WHITE, "b": curses.COLOR_BLUE,
                     "m": curses.COLOR_MAGENTA}
            for i, (k, col) in enumerate(pairs.items(), 1):
                curses.init_pair(i, col, bg)
                A[k] = curses.color_pair(i)
        else:
            for k in "gyrcwbm":
                A[k] = curses.A_NORMAL
        A["G"] = A["g"] | curses.A_BOLD
        A["d"] = A["g"] | curses.A_DIM
        A["R"] = A["r"] | curses.A_BOLD
        A["Y"] = A["y"] | curses.A_BOLD
        A["C"] = A["c"] | curses.A_BOLD
        A["W"] = A["w"] | curses.A_BOLD
        A["i"] = A["g"]
        self.A = A

    # -------------------------------------------------------------- basics
    def put(self, y, x, s, attr=0):
        H, W = self.H, self.W
        if y < 0 or y >= H or x >= W:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        s = s[: W - x - (1 if y == H - 1 else 0)]
        if not s:
            return
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def box(self, y, x, h, w, title="", attr=None):
        a = self.A["d"] if attr is None else attr
        self.put(y, x, "┌" + "─" * (w - 2) + "┐", a)
        for r in range(y + 1, y + h - 1):
            self.put(r, x, "│", a)
            self.put(r, x + w - 1, "│", a)
        self.put(y + h - 1, x, "└" + "─" * (w - 2) + "┘", a)
        if title:
            self.put(y, x + 2, f" {title} ", self.A["G"])

    def bar(self, frac, n, full="█", empty="░"):
        k = int(round(clamp(frac, 0, 1) * n))
        return full * k + empty * (n - k)

    # -------------------------------------------------------------- render
    def render(self, g, now):
        scr = self.scr
        scr.erase()
        self.H, self.W = scr.getmaxyx()
        H, W = self.H, self.W
        if W < 100 or H < 30:
            self.put(0, 0, f"SubZero needs at least 100x30 (now {W}x{H}). Enlarge the terminal.", self.A["Y"])
            scr.refresh()
            return
        dt = 0.0 if self.last_now is None else now - self.last_now
        self.last_now = now
        if g.mode == "brief" and g.world and g.patrol == 1 and g.real < 0.05:
            self.blips.clear()
            self.trails.clear()
        left_w = min(W - 46, max(60, int(W * 0.56)))
        wf_h = max(9, int(H * 0.30))
        ppi_h = H - wf_h
        tele_h = max(18, int(H * 0.58))
        shake = 1 if now < g.shake_until and int(now * 20) % 2 else 0
        flash = now < g.flash_until and int(now * 6) % 2 == 0

        self.draw_ppi(g, 0, 0, ppi_h, left_w, now, dt, flash)
        self.draw_waterfall(g, ppi_h, 0, wf_h, left_w)
        self.draw_telemetry(g, 0, left_w + shake, tele_h, W - left_w - shake, now)
        self.draw_console(g, tele_h, left_w, H - tele_h, W - left_w, now)

        if g.mode == "brief":
            self.draw_brief(g)
        elif g.mode == "debrief":
            self.draw_result(g, True)
        elif g.mode == "over":
            self.draw_result(g, False)
        scr.refresh()

    # ------------------------------------------------------------------ PPI
    def draw_ppi(self, g, y0, x0, h, w, now, dt, flash):
        A = self.A
        wd = g.world
        p = wd.player
        mode = "ACTIVE" if now - g.ping_real < 1.5 else f"PASSIVE {p.sonar}"
        self.box(y0, x0, h, w, f"PPI · {mode} · {g.range} nm · N-UP", A["R"] if flash else None)
        ry = (h - 4) // 2
        rx = min(ry * 2, (w - 8) // 2)
        ry = max(3, min(ry, rx // 2 + 1))
        cy, cx = y0 + h // 2, x0 + w // 2
        R = g.range

        def scr_of(wx, wy):
            dx, dy = wx - p.x, wy - p.y
            return cy - round(dy / R * ry), cx + round(dx / R * rx)

        def inside(row, col):
            return ((col - cx) / rx) ** 2 + ((row - cy) / ry) ** 2 <= 1.0

        occupied = set()

        # bathymetry hazards at current depth (from charts)
        key = (round(p.x, 2), round(p.y, 2), R, int(p.depth // 10), rx, ry)
        if key != self.terrain_key:
            self.terrain_key = key
            cells = []
            ter = wd.terrain
            for r in range(-ry, ry + 1):
                for c in range(-rx, rx + 1):
                    if (c / rx) ** 2 + (r / ry) ** 2 > 0.97:
                        continue
                    wx, wy = p.x + c / rx * R, p.y - r / ry * R
                    if not (0 <= wx <= MAP and 0 <= wy <= MAP):
                        if r % 2 == 0 and c % 4 == 0:
                            cells.append((cy + r, cx + c, "∙"))
                        continue
                    f = ter.floor(wx, wy)
                    if f < p.depth + 30:
                        cells.append((cy + r, cx + c, "▒"))
                    elif f < p.depth + 110:
                        cells.append((cy + r, cx + c, "░"))
            self.terrain_cells = cells
        lit = now - g.ping_real < 2.0
        for r, c, ch in self.terrain_cells:
            a = A["b"] if ch == "∙" else (A["y"] if lit else A["d"])
            if ch == "▒" and not lit:
                a = A["y"] | curses.A_DIM
            self.put(r, c, ch, a)

        # rings, ticks, labels
        for deg in range(0, 360, 3):
            t = math.radians(deg)
            rr, cc = cy - round(math.cos(t) * ry), cx + round(math.sin(t) * rx)
            rel = abs(angdiff(deg, p.heading))
            a = A["r"] | curses.A_DIM if rel > 150 else A["g"]
            if p.sonar == "NARROW" and abs(angdiff(deg, p.narrow_brg)) <= 20:
                a = A["Y"]
            self.put(rr, cc, "·" if deg % 30 else "+", a)
            if deg % 6 == 0:
                rr2, cc2 = cy - round(math.cos(t) * ry * 0.5), cx + round(math.sin(t) * rx * 0.5)
                self.put(rr2, cc2, "·", A["d"])
        self.put(cy - ry - 1, cx - 1, "000", A["G"])
        self.put(cy + ry + 1, cx - 1, "180", A["G"])
        self.put(cy, cx + rx + 2, "090", A["G"])
        self.put(cy, cx - rx - 4, "270", A["G"])
        self.put(cy - ry // 2, cx + 1, f"{R / 2:g}", A["d"])

        # sweep line with fading comet tail
        ang = (now * 360.0 / SWEEP_PERIOD) % 360.0
        tail = [("•", A["G"]), ("∙", A["g"]), ("·", A["g"]), ("·", A["d"]), ("·", A["d"])]
        for k, (ch, a) in reversed(list(enumerate(tail))):
            t = math.radians(ang - k * 4)
            steps = max(rx, ry * 2)
            for s in range(2, steps + 1):
                f = s / steps
                rr, cc = cy - round(math.cos(t) * ry * f), cx + round(math.sin(t) * rx * f)
                self.put(rr, cc, ch, a)

        # refresh blips when the sweep crosses them
        prev = self.prev_sweep
        self.prev_sweep = ang
        swept = (ang - prev) % 360.0
        for tr in wd.tracks.values():
            if tr.is_torp:
                continue
            crossed = (angdiff(tr.brg, prev) % 360.0) <= swept if swept < 180 else True
            fixed = wd.t < tr.fix_until
            pinged = now - g.ping_real < 1.2
            if (crossed and (tr.held or (fixed and tr.ent.alive))) or (pinged and fixed):
                old = self.blips.get(tr.sid)
                if old and old["i"] > 0.05 and (old["x"], old["y"]) != tr.est:
                    tl_ = self.trails.setdefault(tr.sid, [])
                    tl_.insert(0, {"x": old["x"], "y": old["y"], "i": old["i"] * 0.7})
                    del tl_[4:]
                self.blips[tr.sid] = {"x": tr.est[0], "y": tr.est[1], "i": 1.0,
                                      "sym": SYMS.get(tr.cls if not tr.recorded else tr.ent.kind, "?"),
                                      "hostile": (tr.cls if not tr.recorded else tr.ent.kind) in ("warship", "sub"),
                                      "sel": wd.selected is tr}
        decay = dt * 0.2
        for sid in list(self.trails):
            for b in self.trails[sid]:
                b["i"] -= decay * 1.5
            self.trails[sid] = [b for b in self.trails[sid] if b["i"] > 0]
            for b in self.trails[sid]:
                rr, cc = scr_of(b["x"], b["y"])
                if inside(rr, cc):
                    self.put(rr, cc, "∙" if b["i"] > 0.3 else "·", A["d"])
        for sid, b in list(self.blips.items()):
            b["i"] -= decay
            if b["i"] <= 0:
                del self.blips[sid]
                continue
            rr, cc = scr_of(b["x"], b["y"])
            if not inside(rr, cc):
                continue
            base = A["r"] if b["hostile"] else A["g"]
            if b["i"] > 0.6:
                a = base | curses.A_BOLD
            elif b["i"] > 0.25:
                a = base
            else:
                a = base | curses.A_DIM
            ch = b["sym"] if b["i"] > 0.12 else "·"
            if b["sel"] and wd.selected and wd.selected.sid == sid:
                a |= curses.A_REVERSE
            self.put(rr, cc, ch, a)
            if b["i"] > 0.25 and cc + 1 + len(sid) < cx + rx + 3:
                self.put(rr, cc + 1, sid, A["d"])
            occupied.add((rr, cc))

        # weapons: enemy torpedoes we hold, our own wire-guided units & decoys
        blink = int(now * 4) % 2 == 0
        for tr in wd.tracks.values():
            if tr.is_torp and tr.held and tr.ent.alive:
                rr, cc = scr_of(*tr.est)
                if inside(rr, cc):
                    self.put(rr, cc, "*", A["R"] if blink else A["r"])
                    self.put(rr, cc + 1, tr.sid, A["r"])
                else:
                    t = math.radians(tr.brg)
                    self.put(cy - round(math.cos(t) * ry), cx + round(math.sin(t) * rx), "*", A["R"])
        for e in wd.entities:
            if e.side == "own" and e.alive and e.kind in ("torpedo", "decoy"):
                rr, cc = scr_of(e.x, e.y)
                if inside(rr, cc):
                    self.put(rr, cc, "+" if e.kind == "torpedo" else "o", A["C"])
                    if e.kind == "torpedo":
                        self.put(rr, cc + 1, e.name, A["c"])

        # depth charges sinking (we hear the splashes)
        for c in wd.charges:
            rr, cc = scr_of(c[0], c[1])
            if inside(rr, cc):
                self.put(rr, cc, "x", A["R"] if blink else A["r"])

        # enemy active sonar intercepts: red strobe on the bearing
        for b, t0 in g.enemy_pings:
            if now - t0 < 1.5:
                t = math.radians(b)
                for s in range(3, max(rx, 2 * ry) + 1, 2):
                    f = s / max(rx, 2 * ry)
                    self.put(cy - round(math.cos(t) * ry * f), cx + round(math.sin(t) * rx * f), "∙", A["R"])

        # our own ping: expanding ring
        pa = now - g.ping_real
        if 0 <= pa < 1.2:
            f = pa / 1.0
            if f <= 1:
                for deg in range(0, 360, 4):
                    t = math.radians(deg)
                    self.put(cy - round(math.cos(t) * ry * f), cx + round(math.sin(t) * rx * f), "∘", A["C"])

        # exfil marker
        ex, ey = wd.exfil
        rr, cc = scr_of(ex, ey)
        if inside(rr, cc):
            self.put(rr, cc, "X", A["Y"])
        else:
            t = math.radians(bearing(p.x, p.y, ex, ey))
            self.put(cy - round(math.cos(t) * (ry + 0)), cx + round(math.sin(t) * (rx + 1)), "X", A["Y"])

        # own ship and heading
        t = math.radians(p.heading)
        for f in (0.08, 0.15):
            self.put(cy - round(math.cos(t) * ry * f * 1.6), cx + round(math.sin(t) * rx * f), "·", A["C"])
        self.put(cy, cx, "@", A["W"])

        legend = "@ own  ? unk  ! hostile  # neutral  ~ bio  * torp  + ours  o decoy  X exfil"
        self.put(y0 + h - 2, x0 + 2, legend[: w - 4], A["d"])

    # ------------------------------------------------------------ waterfall
    def draw_waterfall(self, g, y0, x0, h, w):
        A = self.A
        wd = g.world
        p = wd.player
        iw = w - 2
        if g.wf == "LOFAR":
            tr = wd.selected
            if tr:
                status = "RECORDED" if tr.recorded else f"REC {tr.intel:3.0f}%"
                title = f"LOFAR · {tr.sid} · 0-600 Hz · {status}"
            else:
                title = "LOFAR · no contact (TRACK Sx / Tab)"
            rows = g.lofar
        else:
            title = "BTR · BEARING/TIME · 000-360"
            rows = g.btr
        self.box(y0, x0, h, w, title)
        # axis
        axis = [" "] * iw
        if g.wf == "LOFAR":
            for hz in range(0, 601, 100):
                c = min(iw - 4, int(hz / 600 * iw))
                for k, ch in enumerate(str(hz)):
                    if c + k < iw:
                        axis[c + k] = ch
        else:
            for deg in range(0, 360, 45):
                c = int(deg / 360 * iw)
                for k, ch in enumerate(f"{deg:03d}"):
                    if c + k < iw:
                        axis[c + k] = ch
        self.put(y0 + 1, x0 + 1, "".join(axis), A["d"])
        if g.wf == "BTR":
            c = int(p.heading / 360 * iw)
            self.put(y0 + 1, x0 + 1 + c, "▼", A["C"])
            for tr in wd.tracks.values():
                if tr.held:
                    c = int(tr.brg / 360 * iw)
                    self.put(y0 + 1, x0 + 1 + c, "▼" if tr.is_torp else "|",
                             A["R"] if tr.is_torp else (A["Y"] if tr is wd.selected else A["g"]))
        n_rows = h - 3
        for r, row in enumerate(list(rows)[:n_rows]):
            line = []
            attrs = []
            for c in range(iw):
                b0 = int(c * BINS / iw)
                b1 = max(b0 + 1, int((c + 1) * BINS / iw))
                v = max(row[b0:b1])
                k = int(clamp(v, 0, 0.999) * len(RAMP))
                line.append(RAMP[k])
                attrs.append(2 if v > 0.75 else 1 if v > 0.4 else 0)
            # draw runs of equal attribute
            start = 0
            for c in range(1, iw + 1):
                if c == iw or attrs[c] != attrs[start]:
                    a = (A["W"], A["G"], A["d"])[2 - attrs[start]] if attrs[start] else A["d"]
                    if attrs[start] == 1:
                        a = A["g"]
                    self.put(y0 + 2 + r, x0 + 1 + start, "".join(line[start:c]), a)
                    start = c

    # ------------------------------------------------------------ telemetry
    def draw_telemetry(self, g, y0, x0, h, w, now):
        A = self.A
        wd = g.world
        p = wd.player
        env = wd.env
        self.box(y0, x0, h, w, "TELEMETRY · SSK-417 MORAY")
        gw = 9                       # depth profile column width
        iw = w - 3 - gw
        x = x0 + 2
        y = y0 + 1
        stress = p.depth > p.test_depth
        jit = (lambda v, s: v + (wd.rng.uniform(-s, s) if stress else 0))

        def line(segs):
            nonlocal y
            cx_ = x
            for txt, a in segs:
                rem = x + iw - cx_
                if rem <= 0:
                    break
                self.put(y, cx_, txt[:rem], a)
                cx_ += len(txt[:rem])
            y += 1

        tc = f"x{g.tc}" if g.tc > 1 else "x1"
        line([(f"PATROL {g.patrol} ", A["G"]), (f"T+{fmt_t(wd.t)} ", A["g"]), (f"TC {tc}", A["Y"] if g.tc > 1 else A["d"]),
              (f"   SCORE {g.run_score + sum(pp for _, pp in wd.score_items)}", A["c"])])
        hd = "" if abs(angdiff(p.ord_hdg, p.heading)) < 1 else f"→{p.ord_hdg:03.0f}"
        sp = "" if abs(p.ord_spd - p.speed) < 0.2 else f"→{p.ord_spd:.0f}"
        dp = "" if abs(p.ord_depth - p.depth) < 1 else f"→{p.ord_depth:.0f}"
        line([("HDG ", A["d"]), (f"{p.heading:03.0f}{hd:<5}", A["W"]), (" SPD ", A["d"]),
              (f"{p.speed:4.1f}{sp:<4}kt", A["W"]), (" DEPTH ", A["d"]), (f"{jit(p.depth, 1.5):5.0f}{dp:<5}m", A["W"])])
        keel = wd.floor_here - p.depth
        kattr = A["R"] if keel < 40 else A["Y"] if keel < 100 else A["g"]
        line([("KEEL ", A["d"]), (f"{keel:5.0f} m", kattr), ("  LAYER ", A["d"]),
              (f"{env.layer_depth:.0f} m ", A["Y"]), (f"{env.side(p.depth)} ", A["G"] if env.side(p.depth) == "BELOW" else A["y"]),
              (f"Δ{env.layer_strength:.0f}dB", A["d"]), (f"  SEA {env.sea_state}", A["d"])])
        n = max(8, iw - 26)
        hull = max(0.0, p.hull)
        ha = A["R"] if hull < 35 else A["Y"] if hull < 70 else A["G"]
        line([("HULL  ", A["d"]), (self.bar(hull / 100, n), ha), (f" {hull:3.0f}%", ha),
              (f" P {jit(p.depth / 10 + 1, 0.4):5.1f} bar", A["R"] if stress else A["g"])])
        line([("      ", A["d"]), (f"test {p.test_depth:.0f} m · crush {p.crush_depth:.0f} m", A["R"] if stress else A["d"])])
        bt = p.battery
        drain = (0.8 if p.silent else 1.5) + (2.5 if p.scrub else 0) + 0.035 * p.speed ** 2 + 0.3
        if p.snorkel_active and p.speed <= 8:
            drain -= 30
        ba = A["R"] if bt < 15 else A["Y"] if bt < 35 else A["G"]
        line([("BATT  ", A["d"]), (self.bar(bt / 100, n), ba), (f" {bt:3.0f}%", ba),
              (f" {-drain:+5.1f}%/h", A["c"] if drain < 0 else A["d"])])
        ca = A["R"] if p.co2 > 3 else A["Y"] if p.co2 > 1.5 else A["g"]
        line([("ATMOS ", A["d"]), (f"O2 {p.o2:4.1f}%  ", A["g"]), (f"CO2 {p.co2:3.1f}%  ", ca),
              ("SCRUB ", A["d"]), ("ON " if p.scrub else "OFF", A["G"] if p.scrub else A["Y"])])
        sl = p.sl(wd.t)
        na = A["R"] if sl > 145 else A["Y"] if sl > 133 else A["G"]
        line([("NOISE ", A["d"]), (self.bar((sl - 115) / 45, n, "▮", "▯"), na), (f" {sl:3.0f} dB", na)])
        flags = []
        if p.cavitating():
            flags.append(("CAVITATING ", A["R"]))
        if p.silent:
            flags.append(("SILENT RUN ", A["C"]))
        if p.snorkel:
            flags.append(("SNORKEL " + ("ON " if p.snorkel_active else "(PD!) "), A["Y"]))
        if wd.p_canyon:
            flags.append(("CANYON MASK ", A["C"]))
        if wd.p_bottom:
            flags.append(("BOTTOM MASK ", A["c"]))
        if p.impaired():
            flags.append(("CREW IMPAIRED ", A["R"]))
        line([("      ", A["d"])] + (flags or [("— nominal —", A["d"])]))
        sm = f"PASSIVE {p.sonar}" + (f" {p.narrow_brg:03.0f}" if p.sonar == "NARROW" else "")
        line([("SONAR ", A["d"]), (sm, A["G"]), (f"  RANGE {g.range} nm", A["d"]),
              ("  DEAF" if wd.t < wd.deafen_until else "", A["R"])])
        segs = [("TUBES ", A["d"])]
        for i, tb in enumerate(p.tubes):
            if tb["load"] is None:
                segs.append((f"{i + 1}:----  ", A["d"]))
            elif tb["timer"] > 0:
                segs.append((f"{i + 1}:{tb['load'][0]}{tb['timer']:.0f}s ", A["y"]))
            else:
                segs.append((f"{i + 1}:{tb['load']} ", A["C"] if tb["load"] == "TORP" else A["c"]))
        segs.append((f" RACK T{p.torps} D{p.decoys}", A["d"]))
        line(segs)
        ex, ey = wd.exfil
        rec = sum(1 for tr in wd.tracks.values() if tr.recorded and tr.ent.kind in ("warship", "sub"))
        line([("EXFIL ", A["d"]), (f"brg {bearing(p.x, p.y, ex, ey):03.0f} · {math.hypot(ex - p.x, ey - p.y):4.1f} nm", A["Y"]),
              ("   INTEL ", A["d"]), (f"{rec}/{wd.need}", A["G"] if rec >= wd.need else A["Y"])])

        # alerts
        blink = int(now * 3) % 2 == 0
        alerts = []
        for tr in wd.tracks.values():
            if tr.is_torp and tr.held and tr.ent.alive:
                lock = " LOCKED ON US" if tr.ent.target is p else ""
                alerts.append((f"⚠ {tr.sid} TORPEDO brg {tr.brg:03.0f} ~{tr.rng_est:.1f} nm{lock}", A["R"] if blink else A["r"]))
        if wd.charges:
            alerts.append(("⚠ DEPTH CHARGES SINKING", A["R"] if blink else A["r"]))
        if wd.shoal_ahead:
            alerts.append(("⚠ OBSTACLE AHEAD — CHANGE DEPTH/COURSE", A["Y"]))
        if stress:
            alerts.append(("⚠ BELOW TEST DEPTH", A["R"] if blink else A["y"]))
        if p.battery < 15:
            alerts.append(("⚠ BATTERY LOW — SNORKEL AT PD", A["Y"]))
        if math.hypot(p.x - MAP / 2, p.y - MAP / 2) > MAP * 0.72:
            alerts.append(("⚠ LEAVING PATROL AREA", A["y"]))
        for txt, a in alerts[:3]:
            line([(txt, a)])

        # contacts
        if y < y0 + h - 2:
            self.put(y, x, "─ CONTACTS " + "─" * max(0, iw - 11), A["d"])
            y += 1
        for tr in g.sorted_tracks():
            if y >= y0 + h - 1:
                break
            sel = tr is wd.selected
            if tr.is_torp:
                if not tr.ent.alive:
                    continue
                cls, ca = "TORPEDO", A["R"]
            elif tr.recorded:
                cls, ca = CLASS_NAMES[tr.ent.kind] + "✓", A["C"]
            elif tr.cls:
                cls, ca = CLASS_NAMES[tr.cls] + "?", A["y"] if tr.cls in ("warship", "sub") else A["g"]
            else:
                cls, ca = "UNKNOWN", A["d"]
            if tr.sunk:
                st, sa = "SUNK", A["c"]
            elif tr.lost:
                st, sa = "LOST", A["d"]
            elif tr.held:
                st, sa = f"{tr.se:+3.0f}dB", A["G"] if tr.se > 6 else A["g"]
            else:
                st, sa = "fade", A["d"]
            rng = f"{tr.rng_est:4.1f}" if wd.t < tr.fix_until else f"~{tr.rng_est:.0f}" if tr.rng_est >= 10 else f"~{tr.rng_est:3.1f}"
            intel = f" {tr.intel:3.0f}%" if 0 < tr.intel < 100 else ""
            line([("►" if sel else " ", A["Y"]), (f"{tr.sid:<4}", A["W"] if sel else A["g"]),
                  (f"{tr.brg:03.0f}° {rng}nm ", A["g"] if tr.held else A["d"]), (f"{st:<6} ", sa),
                  (f"{cls:<11}", ca), (intel, A["c"])])

        self.draw_depth_profile(g, y0 + 1, x0 + w - 1 - gw, h - 2, gw)

    def draw_depth_profile(self, g, y0, x0, h, w):
        A = self.A
        wd = g.world
        p = wd.player
        maxd = 700.0
        rows = h - 1

        def row_of(d):
            return y0 + 1 + int(clamp(d / maxd, 0, 0.999) * rows)

        for r in range(rows):
            self.put(y0 + 1 + r, x0, "│", A["d"])
        self.put(y0, x0, "│DEPTH ", A["d"])
        self.put(row_of(0), x0 + 1, "≈≈≈≈≈≈≈", A["b"])
        floor_r = row_of(wd.floor_here)
        for r in range(floor_r, y0 + 1 + rows):
            self.put(r, x0 + 1, "▓▓▓▓▓▓▓", A["y"] | curses.A_DIM)
        self.put(row_of(wd.env.layer_depth), x0 + 1, "═LAYER═", A["Y"])
        self.put(row_of(p.test_depth), x0 + 1, "-test--", A["y"])
        self.put(row_of(p.crush_depth), x0 + 1, "✕CRUSH✕", A["R"])
        self.put(row_of(p.ord_depth), x0, ">", A["C"])
        self.put(row_of(p.depth), x0 + 1, f" @{p.depth:4.0f}", A["W"])

    # -------------------------------------------------------------- console
    def draw_console(self, g, y0, x0, h, w, now):
        A = self.A
        self.box(y0, x0, h, w, "CONN · COMMAND / EVENT LOG")
        iw = w - 4
        lines = []
        for stamp, text, lvl in g.logs:
            wrapped = textwrap.wrap(text, iw - 9) or [""]
            for k, ln in enumerate(wrapped):
                lines.append(((stamp if k == 0 else "") + " " * (9 - (len(stamp) if k == 0 else 0)) + ln, lvl))
        avail = h - 4
        end = len(lines) - g.log_scroll
        start = max(0, end - avail)
        for i, (ln, lvl) in enumerate(lines[start:end]):
            a = {"w": A["W"], "d": A["d"], "y": A["Y"], "r": A["r"], "R": A["R"], "c": A["c"],
                 "g": A["G"], "i": A["g"]}.get(lvl, A["g"])
            self.put(y0 + 1 + i, x0 + 2, ln[:iw], a)
        if g.log_scroll:
            self.put(y0 + 1, x0 + w - 12, f"[↑{g.log_scroll}]", A["Y"])
        self.put(y0 + h - 3, x0 + 1, "─" * (w - 2), A["d"])
        cur = "█" if int(now * 2) % 2 == 0 else " "
        prompt = "CONN> "
        text = g.input[-(iw - len(prompt) - 1):]
        self.put(y0 + h - 2, x0 + 2, prompt, A["G"])
        self.put(y0 + h - 2, x0 + 2 + len(prompt), text + cur, A["W"])

    # -------------------------------------------------------------- overlays
    def overlay(self, lines, width=None):
        A = self.A
        width = width or max(len(t) for t, _ in lines) + 6
        h = len(lines) + 2
        y0 = max(0, (self.H - h) // 2)
        x0 = max(0, (self.W - width) // 2)
        for r in range(h):
            self.put(y0 + r, x0, " " * width, A["g"])
        self.box(y0, x0, h, width, attr=A["G"])
        for i, (t, a) in enumerate(lines):
            self.put(y0 + 1 + i, x0 + 3, t, a)

    def draw_brief(self, g):
        A = self.A
        wd = g.world
        env = wd.env
        p = wd.player
        L = [(t, A["G"]) for t in TITLE]
        L += [("", 0),
              (f"PATROL {g.patrol} — SSK-417 MORAY · seed {g.master_seed}", A["W"]),
              ("", 0),
              ("You are the sonar & acoustics operator of an ageing diesel-electric", A["g"]),
              ("research/intelligence boat, alone below the layer. You see nothing.", A["g"]),
              ("You only listen.", A["g"]),
              ("", 0),
              (f"ORDERS: record the acoustic signatures of {wd.need} hostile military", A["Y"]),
              ("        unit(s) (TRACK a contact, hold it on LOFAR), then reach the", A["Y"]),
              (f"        exfil point X, {math.hypot(wd.exfil[0] - p.x, wd.exfil[1] - p.y):.0f} nm "
               f"bearing {bearing(p.x, p.y, *wd.exfil):03.0f}. Do NOT sink neutrals.", A["Y"]),
              ("", 0),
              (f"INTEL: layer at ~{env.layer_depth:.0f} m, sea state {env.sea_state}. "
               f"Hide below it. Pinging gives you away.", A["c"]),
              ("       Type HELP for orders and LIB for the signature library.", A["c"]),
              ("", 0),
              ("       [ENTER] dive          [Q] quit", A["G"])]
        self.overlay(L)

    def draw_result(self, g, success):
        A = self.A
        r = g.result
        L = []
        if success:
            L.append((f"PATROL {g.patrol} COMPLETE", A["G"]))
        else:
            L.append(("— LOST AT SEA —" if r["kind"] == "dead" else "— PATROL OVER —", A["R"]))
        L.append((r["reason"], A["Y"]))
        L.append((f"Time submerged: {fmt_t(r['time'])}   Signatures: {r['rec']}/{r['need']}", A["g"]))
        L.append(("", 0))
        for label, pts in r["items"][-12:]:
            L.append((f"{label[:44]:<44} {pts:+6d}", A["G"] if pts >= 0 else A["R"]))
        if not r["items"]:
            L.append(("(nothing of note)", A["d"]))
        L.append(("", 0))
        L.append((f"{'Patrol total':<44} {r['patrol_pts']:+6d}", A["W"]))
        L.append((f"{'RUN SCORE':<44} {g.run_score:6d}", A["W"]))
        L.append(("", 0))
        if success:
            L.append(("The tender rearms you; the hull is patched. Another patrol awaits.", A["c"]))
            L.append(("[ENTER] next patrol", A["G"]))
        else:
            L.append(("HALL OF THE DEEP", A["G"]))
            for i, s in enumerate(g.scores[:6]):
                L.append((f"{i + 1}. {s.get('score', 0):6d}  P{s.get('patrol', 1)}  {s.get('date', '')}  "
                          f"{s.get('cause', '')[:28]}", A["g"]))
            L.append(("", 0))
            L.append(("[R] new run (permadeath — new ocean)     [Q] quit", A["G"]))
        self.overlay(L, 64)
