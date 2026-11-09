"""Simulation core: ocean environment, bathymetry, acoustics, entities and AI.

Units: positions in nautical miles (x east, y north), depth in metres,
speed in knots, time in seconds, bearings in degrees true (0 = north).
"""
from __future__ import annotations

import itertools
import math
import random

NM_M = 1852.0
MAP = 40.0          # patrol box is MAP x MAP nm
DI_OWN = 15.0       # own array directivity index
DT_DET = 5.0        # detection threshold
SUBSTEP = 0.5       # physics step (s)

# ----------------------------------------------------------------- helpers


def tl(r_nm: float) -> float:
    """Transmission loss: spherical spreading + absorption."""
    r = max(r_nm, 0.02)
    return 20.0 * math.log10(r * 2000.0) + 0.3 * r


def psum(*levels: float) -> float:
    return 10.0 * math.log10(sum(10 ** (v / 10.0) for v in levels))


def bearing(x1, y1, x2, y2) -> float:
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360.0


def angdiff(a, b) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def dist(a, b) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def cav_speed(depth: float) -> float:
    return 7.0 + max(depth, 0.0) / 30.0


CLASS_NAMES = {
    "merchant": "MERCHANT", "trawler": "TRAWLER", "whale": "BIOLOGIC",
    "warship": "WARSHIP", "sub": "SUBMARINE", "torpedo": "TORPEDO", "decoy": "DECOY",
}
CLASS_ALIASES = {
    "MERCHANT": "merchant", "MERCH": "merchant", "CARGO": "merchant", "M": "merchant",
    "TRAWLER": "trawler", "FISHING": "trawler", "FISH": "trawler", "TRAWL": "trawler", "T": "trawler",
    "BIOLOGIC": "whale", "BIO": "whale", "WHALE": "whale", "B": "whale",
    "WARSHIP": "warship", "WAR": "warship", "DESTROYER": "warship", "FRIGATE": "warship",
    "DD": "warship", "FF": "warship", "W": "warship",
    "SUBMARINE": "sub", "SUB": "sub", "SSN": "sub", "SS": "sub", "S": "sub",
    "UNKNOWN": None, "U": None, "?": None, "CLEAR": None,
}
SURFACE_KINDS = ("merchant", "trawler", "warship")
INTEL_PTS = {"merchant": 40, "trawler": 40, "whale": 60, "warship": 300, "sub": 500}
KILL_PTS = {"warship": 400, "sub": 600, "merchant": -800, "trawler": -800, "whale": -300}

# ------------------------------------------------------------- environment


class Environment:
    def __init__(self, rng: random.Random):
        self.layer_depth = rng.uniform(150, 330)
        self.layer_strength = rng.uniform(10, 17)
        self.sea_state = rng.randint(1, 6)
        self.ambient = 59.0 + 1.6 * self.sea_state

    def layer_loss(self, d1, d2) -> float:
        if (d1 < self.layer_depth) != (d2 < self.layer_depth):
            return self.layer_strength
        return 0.0

    def side(self, d) -> str:
        return "ABOVE" if d < self.layer_depth else "BELOW"


class Terrain:
    """Procedural bathymetry: value noise plateaus, seamounts and canyons."""

    RES = 0.25

    def __init__(self, rng: random.Random, size: float = MAP):
        self.size = size
        n = self.n = int(size / self.RES) + 1
        o1 = self._noise(rng, 5)
        o2 = self._noise(rng, 13)
        mounts = [(rng.uniform(4, size - 4), rng.uniform(4, size - 4),
                   rng.uniform(0.8, 2.0), rng.uniform(140, 300))
                  for _ in range(rng.randint(2, 4))]
        canyons = []
        for _ in range(rng.randint(1, 3)):
            a = rng.uniform(0, math.pi)
            canyons.append((rng.uniform(10, 30), rng.uniform(10, 30), math.cos(a), math.sin(a),
                            rng.uniform(1.0, 3.5), rng.uniform(0.12, 0.35), rng.uniform(0, 6.28),
                            rng.uniform(0.35, 0.7), rng.uniform(260, 420)))
        base = rng.uniform(560, 660)
        grid = []
        for j in range(n):
            y = j * self.RES
            row = []
            for i in range(n):
                x = i * self.RES
                d = base + 260 * self._sample(o1, x, y) + 80 * self._sample(o2, x, y)
                for cx, cy, ux, uy, amp, fr, ph, w, dep in canyons:
                    px, py = x - cx, y - cy
                    along = px * ux + py * uy
                    across = -px * uy + py * ux
                    off = across - amp * math.sin(along * fr + ph)
                    d += dep * math.exp(-(off / w) ** 2)
                for mx, my, s, peak in mounts:
                    if d > peak:
                        f = math.exp(-((x - mx) ** 2 + (y - my) ** 2) / (2 * s * s))
                        d -= (d - peak) * f
                row.append(max(40.0, d))
            grid.append(row)
        self.grid = grid

    def _noise(self, rng, cells):
        return [[rng.uniform(-1, 1) for _ in range(cells + 2)] for _ in range(cells + 2)], self.size / cells

    @staticmethod
    def _sample(o, x, y):
        g, c = o
        fx, fy = x / c, y / c
        i, j = int(fx), int(fy)
        tx, ty = fx - i, fy - j
        tx, ty = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
        a = g[j][i] + (g[j][i + 1] - g[j][i]) * tx
        b = g[j + 1][i] + (g[j + 1][i + 1] - g[j + 1][i]) * tx
        return a + (b - a) * ty

    def floor(self, x, y) -> float:
        fx = clamp(x, 0, self.size - 1e-6) / self.RES
        fy = clamp(y, 0, self.size - 1e-6) / self.RES
        i, j = int(fx), int(fy)
        tx, ty = fx - i, fy - j
        g = self.grid
        i1, j1 = min(i + 1, self.n - 1), min(j + 1, self.n - 1)
        a = g[j][i] + (g[j][i1] - g[j][i]) * tx
        b = g[j1][i] + (g[j1][i1] - g[j1][i]) * tx
        return a + (b - a) * ty

    def rim(self, x, y, r=0.8) -> float:
        return min(self.floor(x + r * math.sin(a), y + r * math.cos(a))
                   for a in (k * math.pi / 4 for k in range(8)))


# ---------------------------------------------------------------- entities

_uid = itertools.count(1)


class Entity:
    kind = "generic"
    side = "neutral"
    sl0 = 130.0
    slk = 0.0
    ts = 15.0
    can_cavitate = False

    def __init__(self, x, y, depth, heading, speed, rng):
        self.uid = next(_uid)
        self.rng = rng
        self.x, self.y, self.depth = x, y, depth
        self.heading = heading % 360.0
        self.speed = speed
        self.ord_hdg, self.ord_spd, self.ord_depth = self.heading, speed, depth
        self.turn_rate, self.accel, self.dive_rate = 3.0, 0.3, 2.0
        self.alive = True
        self.sunk = False
        # what our sonar currently makes of it (refreshed each sensor tick)
        self.se_own = -99.0
        self.brg_own = 0.0
        self.r_own = 99.0
        self.baffled = False
        self.pend = 0
        self.tonals: list[tuple[float, float]] = []
        self.bb = 0.1
        self.phase = rng.uniform(0, 6.28)

    def cavitating(self) -> bool:
        return self.can_cavitate and self.speed > cav_speed(self.depth)

    def sl(self, t) -> float:
        v = self.sl0 + self.slk * self.speed
        if self.cavitating():
            v += 15.0
        return v

    def steer(self, dt):
        d = angdiff(self.ord_hdg, self.heading)
        step = self.turn_rate * dt
        self.heading = (self.heading + clamp(d, -step, step)) % 360.0
        ds = self.ord_spd - self.speed
        a = self.accel * dt * (1.6 if ds < 0 else 1.0)
        self.speed = max(0.0, self.speed + clamp(ds, -a, a))
        dz = self.ord_depth - self.depth
        self.depth += clamp(dz, -self.dive_rate * dt, self.dive_rate * dt)

    def move(self, dt):
        self.steer(dt)
        d = self.speed / 3600.0 * dt
        h = math.radians(self.heading)
        self.x += d * math.sin(h)
        self.y += d * math.cos(h)

    def update(self, w, dt):
        self.move(dt)

    def think(self, w):
        pass

    def lines(self, t):
        """Narrowband tonals (Hz, amplitude) and broadband level for LOFAR."""
        return self.tonals, self.bb

    def keep_in_box(self, margin=3.0):
        if not (margin < self.x < MAP - margin and margin < self.y < MAP - margin):
            self.ord_hdg = bearing(self.x, self.y, MAP / 2, MAP / 2) + self.rng.uniform(-40, 40)


class Merchant(Entity):
    kind = "merchant"
    sl0, slk, ts = 146.0, 0.8, 25.0

    def __init__(self, x, y, heading, rng):
        super().__init__(x, y, 0.0, heading, rng.uniform(10, 16), rng)
        f = rng.uniform(45, 70)
        self.tonals = [(f, .9), (2 * f, .75), (3 * f, .6), (4 * f, .45), (5 * f, .3)]
        self.bb = 0.35
        self.turn_rate = 0.5

    def think(self, w):
        if not (-6 < self.x < MAP + 6 and -6 < self.y < MAP + 6):
            w.respawn_merchant(self)


class Trawler(Entity):
    kind = "trawler"
    sl0, slk, ts = 139.0, 1.2, 18.0

    def __init__(self, x, y, heading, rng):
        super().__init__(x, y, 0.0, heading, rng.uniform(3, 6), rng)
        g = rng.choice([100.0, 120.0]) + rng.uniform(-1.5, 1.5)
        self.tonals = [(g, .8), (2 * g, .5)]
        self.winch = rng.uniform(300, 450)
        self.bb = 0.2
        self.turn_rate = 1.0

    def lines(self, t):
        ln = list(self.tonals)
        if math.sin(t / 40.0 + self.phase) > 0.3:
            ln.append((self.winch, .7))
        return ln, self.bb

    def think(self, w):
        if self.rng.random() < 1 / 300:
            self.ord_hdg = self.rng.uniform(0, 360)
        self.keep_in_box()


class Whale(Entity):
    kind = "whale"
    side = "bio"
    sl0, slk, ts = 132.0, 0.0, 10.0

    def __init__(self, x, y, rng):
        super().__init__(x, y, rng.uniform(20, 150), rng.uniform(0, 360), rng.uniform(2, 4), rng)
        self.bb = 0.05

    def singing(self, t):
        return math.sin(t / 35.0 + self.phase) > 0

    def sl(self, t):
        return self.sl0 + (8.0 if self.singing(t) else 0.0)

    def lines(self, t):
        bb = 0.55 if self.rng.random() < 0.25 else 0.04   # clicks
        if not self.singing(t):
            return [], bb
        f = 120 + 80 * math.sin(t * 0.35 + self.phase) + 40 * math.sin(t * 1.3)
        return [(f, .9), (f * 1.9, .4)], bb

    def think(self, w):
        if self.rng.random() < 1 / 60:
            self.ord_hdg = (self.heading + self.rng.uniform(-90, 90)) % 360
            self.ord_depth = self.rng.uniform(10, 200)
        self.keep_in_box(1.0)


class Decoy(Entity):
    kind = "decoy"
    sl0, ts = 150.0, 12.0

    def __init__(self, side, x, y, depth, heading, rng, life=150.0):
        super().__init__(x, y, depth, heading, 4.0, rng)
        self.side = side
        self.life = life
        self.bb = 0.9

    def sl(self, t):
        return self.sl0

    def update(self, w, dt):
        self.move(dt)
        self.life -= dt
        if self.life <= 0:
            self.alive = False


class Torpedo(Entity):
    kind = "torpedo"
    sl0, slk, ts = 150.0, 0.3, 5.0

    def __init__(self, side, x, y, depth, heading, speed, enable, run_max, search_depth, rng, name):
        super().__init__(x, y, depth, heading, speed, rng)
        self.side = side
        self.name = name
        self.enable = enable
        self.run_max = run_max
        self.state = "run" if enable > 0 else "search"
        self.traveled = 0.0
        self.target = None
        self.acq_cd = 0.0
        self.turn_rate, self.accel, self.dive_rate = 25.0, 5.0, 12.0
        self.ord_depth = search_depth
        self.search_base = self.heading
        f = rng.uniform(1500, 1700) / 4
        self.tonals = [(f, 1.0), (f * 0.5, .8)]
        self.bb = 0.8
        self.locked_player = False

    def valid_target(self, c, w):
        if c is self or not c.alive or c.kind == "torpedo":
            return False
        if self.side == "own":
            return c is not w.player and c.side != "own"
        return c is w.player or (c.kind == "decoy" and c.side == "own")

    def seek(self, w):
        best, best_score = None, 45.0
        for c in w.entities:
            if not self.valid_target(c, w):
                continue
            r = dist(self, c)
            if r > 2.0:
                continue
            if abs(angdiff(bearing(self.x, self.y, c.x, c.y), self.heading)) > 50:
                continue
            layer = w.env.layer_loss(self.depth, c.depth)
            if r > (2.0 if layer == 0 else 0.9):
                continue
            score = c.sl(w.t) - tl(r) - layer + c.ts * 0.5
            if c is w.player:
                score -= w.p_mask * 0.5
            if score > best_score:
                best, best_score = c, score
        if best is not self.target:
            w.torpedo_lock_change(self, self.target, best)
        self.target = best

    def update(self, w, dt):
        if self.target is not None and not self.target.alive:
            self.target = None
        self.acq_cd -= dt
        if self.state != "run" and self.acq_cd <= 0:
            self.acq_cd = 0.5
            self.seek(w)
        if self.target is not None:
            self.ord_hdg = bearing(self.x, self.y, self.target.x, self.target.y)
            self.ord_depth = self.target.depth
        elif self.state == "search":
            self.search_base = (self.search_base + 4.0 * dt) % 360
            self.ord_hdg = self.search_base + 40 * math.sin(self.traveled * 6)
        self.move(dt)
        self.traveled += self.speed / 3600.0 * dt
        if self.state == "run" and self.traveled >= self.enable:
            self.state = "search"
            self.search_base = self.heading
        if self.traveled > self.run_max:
            self.alive = False
            w.torpedo_spent(self)
            return
        t = self.target
        if t is not None:
            d3 = math.hypot(dist(self, t) * NM_M, t.depth - self.depth)
            if d3 < 45:
                w.detonate(self, t)


# ------------------------------------------------------------ hostile AI


class Hostile(Entity):
    side = "hostile"
    di = 15.0
    patrol_spd, hunt_spd, evade_spd = 12.0, 18.0, 25.0

    def __init__(self, x, y, depth, heading, speed, rng, area):
        super().__init__(x, y, depth, heading, speed, rng)
        self.area = area
        self.state = "patrol"
        self.known = None
        self.known_err = 9.0
        self.known_depth = 100.0
        self.contact = 0.0
        self.last_contact = -1e9
        self.fire_cd = rng.uniform(20, 60)
        self.weapons = 4
        self.evade_until = -1.0
        self.wp = None

    def sensor_depth(self, w):
        return self.depth

    def self_noise(self):
        return 45.0 + 1.3 * self.speed

    def nl(self, w):
        return psum(w.ambient(), self.self_noise())

    def set_datum(self, w, x, y, err, depth, strength):
        self.contact = min(20.0, self.contact + strength)
        self.last_contact = w.t
        self.known = (x + self.rng.gauss(0, err), y + self.rng.gauss(0, err))
        self.known_err = err
        self.known_depth = depth
        if self.state == "patrol" and self.contact >= 3:
            self.state = "hunt"
            self.on_hunt(w)

    def register(self, w, err, precise=False, strength=1.0):
        p = w.player
        if precise:
            depth = p.depth + self.rng.gauss(0, 25)
        else:
            depth = self.guess_depth(w)
        self.set_datum(w, p.x, p.y, err, depth, strength)

    def guess_depth(self, w):
        L = w.env.layer_depth
        if w.env.side(self.sensor_depth(w)) == "ABOVE":
            return self.rng.uniform(40, L - 20)
        return self.rng.uniform(L + 20, L + 220)

    def on_hunt(self, w):
        pass

    def on_evade(self, w, tp):
        self.ord_hdg = (bearing(tp.x, tp.y, self.x, self.y) + self.rng.uniform(-30, 30)) % 360
        self.ord_spd = self.evade_spd

    def think(self, w):
        self.fire_cd -= 1
        p = w.player
        for tp in w.entities:
            if (tp.kind == "torpedo" and tp.side == "own" and tp.alive
                    and tp.state != "run" and dist(self, tp) < 3.0):
                if self.evade_until < w.t:
                    self.on_evade(w, tp)
                self.evade_until = w.t + 45
                break
        r = dist(self, p)
        layer = w.env.layer_loss(self.sensor_depth(w), p.depth)
        se = (p.sl(w.t) - tl(r) - layer - w.p_mask - self.nl(w) + self.di - DT_DET
              + self.rng.gauss(0, 3))
        if se > 0:
            err = clamp(r * 0.35 / (1 + se / 4), 0.15, 3.0)
            self.register(w, err, strength=1.0 + se / 10)
        if p.snorkel_active and self.kind == "warship" and r < 6 and self.rng.random() < 0.15:
            self.register(w, 0.2, precise=True, strength=4)   # radar/visual on the mast
        since = w.t - self.last_contact
        if since > 90:
            self.contact = max(0.0, self.contact - 0.1)
        if self.evade_until > w.t:
            return
        self.behave(w, since)

    def behave(self, w, since):
        if self.state == "patrol" or self.known is None:
            self.state = "patrol"
            self.ord_spd = self.patrol_spd
            if self.wp is None or math.hypot(self.wp[0] - self.x, self.wp[1] - self.y) < 0.6:
                cx, cy, rad = self.area
                a, rr = self.rng.uniform(0, 6.283), rad * math.sqrt(self.rng.random())
                self.wp = (clamp(cx + rr * math.sin(a), 2, MAP - 2), clamp(cy + rr * math.cos(a), 2, MAP - 2))
            self.ord_hdg = bearing(self.x, self.y, *self.wp)
            return
        if since > 300:
            self.state, self.contact, self.known, self.wp = "patrol", 0.0, None, None
            return
        kx, ky = self.known
        d = math.hypot(kx - self.x, ky - self.y)
        if d > 0.4:
            self.ord_hdg = bearing(self.x, self.y, kx, ky)
        else:
            self.ord_hdg = (self.heading + 25) % 360
        self.ord_spd = self.hunt_spd if d > 2.5 else self.patrol_spd
        self.attack(w, d, since)

    def attack(self, w, d, since):
        pass


class Warship(Hostile):
    kind = "warship"
    sl0, slk, ts = 139.0, 0.65, 22.0
    di = 14.0
    patrol_spd, hunt_spd, evade_spd = 12.0, 20.0, 28.0

    def __init__(self, x, y, heading, rng, area, vds_depth=None):
        super().__init__(x, y, 0.0, heading, 12.0, rng, area)
        self.vds_depth = vds_depth
        self.dc = 12
        self.dc_cd = 0
        self.ping_cd = 0
        self.turn_rate = 2.0
        t = rng.uniform(320, 380)
        g = rng.uniform(170, 200)
        b = rng.uniform(25, 35)
        self.tonals = [(t, .9), (g, .6), (b, .7), (2 * b, .45)]
        self.bb = 0.3

    def sensor_depth(self, w):
        return self.vds_depth if self.vds_depth is not None else 8.0

    def behave(self, w, since):
        super().behave(w, since)
        self.dc_cd -= 1
        if self.state == "hunt":
            self.ping_cd -= 1
            if self.ping_cd <= 0:
                self.ping_cd = self.rng.uniform(12, 20)
                w.enemy_ping(self)

    def attack(self, w, d, since):
        if since > 25 or self.known_err > 0.8:
            return
        if d < 0.35 and self.dc > 0 and self.dc_cd <= 0:
            self.dc_cd = 40
            n = min(5, self.dc)
            self.dc -= n
            w.drop_charges(self, n)
        elif 1.0 < d < 5.0 and self.weapons > 0 and self.fire_cd <= 0:
            self.fire_cd = 100
            self.weapons -= 1
            w.asroc(self)


class Sub(Hostile):
    kind = "sub"
    sl0, slk, ts = 123.0, 1.0, 14.0
    can_cavitate = True
    di = 18.0
    patrol_spd, hunt_spd, evade_spd = 5.0, 9.0, 20.0

    def __init__(self, x, y, depth, heading, rng, area):
        super().__init__(x, y, depth, heading, 5.0, rng, area)
        self.decoys = 1
        self.weapons = 3
        self.turn_rate = 2.5
        f = rng.choice([50.0, 60.0]) + rng.uniform(-0.5, 0.5)
        self.tonals = [(f, .5), (2 * f, .35), (rng.uniform(230, 270), .3)]
        self.bb = 0.05

    def on_hunt(self, w):
        self.ord_depth = clamp(self.known_depth, 40, 420)

    def on_evade(self, w, tp):
        super().on_evade(w, tp)
        L = w.env.layer_depth
        self.ord_depth = L + 80 if self.depth < L else max(40, L - 60)
        if self.decoys > 0:
            self.decoys -= 1
            w.spawn_decoy(self, "hostile", self.heading + 180)

    def attack(self, w, d, since):
        if (since > 15 or self.known_err > 0.5 or self.contact < 6 or self.weapons <= 0
                or self.fire_cd > 0):
            return
        if d < 5.0:
            self.fire_cd = 240
            self.weapons -= 1
            w.hostile_launch(self, bearing(self.x, self.y, *self.known), max(0.3, d - 1.0))


# ------------------------------------------------------------------ player


class Player(Entity):
    kind = "own"
    side = "own"
    ts = 10.0
    can_cavitate = True
    max_speed = 20.0

    def __init__(self, x, y, depth, heading, rng, loadout):
        super().__init__(x, y, depth, heading, 4.0, rng)
        self.hull = loadout["hull"]
        self.battery = loadout["battery"]
        self.torps = loadout["torps"]
        self.decoys = loadout["decoys"]
        self.tubes = [{"load": "TORP", "timer": 0.0}, {"load": "TORP", "timer": 0.0},
                      {"load": "DECOY", "timer": 0.0}, {"load": None, "timer": 0.0}]
        self.o2, self.co2 = 20.9, 0.4
        self.silent = False
        self.scrub = True
        self.snorkel = False
        self.sonar = "BROAD"
        self.narrow_brg = 0.0
        self.turn_rate, self.accel, self.dive_rate = 2.0, 0.12, 2.0

    @property
    def snorkel_active(self):
        return self.snorkel and self.depth <= 20.5

    @property
    def frame(self):
        return 0.6 + 0.4 * max(self.hull, 0) / 100.0

    @property
    def test_depth(self):
        return 450.0 * self.frame

    @property
    def crush_depth(self):
        return 650.0 * self.frame

    def impaired(self):
        return self.co2 > 3.0

    def sl(self, t):
        v = 124.0 + 1.0 * self.speed
        if self.scrub:
            v += 2.0
        if self.silent:
            v -= 6.0
        if self.cavitating():
            v += 15.0
        if self.snorkel_active:
            v += 26.0
        return v

    def self_noise(self):
        return 47.0 + 1.3 * self.speed + (12.0 if self.snorkel_active else 0.0)

    def steer(self, dt):
        if self.battery <= 0:
            self.ord_spd = 0.0
        k = 0.5 if self.impaired() else 1.0
        self.turn_rate = 2.0 * k * clamp(self.speed / 3.0, 0.3, 1.0)
        self.accel = 0.12 * k
        self.dive_rate = (2.0 if self.speed >= 1 else 0.6) * k
        super().steer(dt)


# ------------------------------------------------------------------ tracks


class Track:
    def __init__(self, sid, ent, t, rng, p):
        self.sid, self.ent = sid, ent
        self.first = self.last = t
        self.lost = False
        self.held = False
        self.sunk = False
        self.cls = "torpedo" if ent.kind == "torpedo" else None
        self.recorded = False
        self.intel = 0.0
        self.err = 0.6
        self.g = rng.uniform(-1, 1)
        self.rng = rng
        self.fix_until = -1.0
        self.leg_hdg = p.heading
        self.se = -99.0
        self.brg = 0.0
        self.rng_est = 0.0
        self.est = (ent.x, ent.y)
        self.spd_ref = ent.speed

    @property
    def is_torp(self):
        return self.ent.kind == "torpedo"

    def observe(self, p, t, se):
        e = self.ent
        r = math.hypot(e.x - p.x, e.y - p.y)
        b = bearing(p.x, p.y, e.x, e.y)
        self.held, self.last, self.se, self.lost = True, t, se, False
        if abs(angdiff(p.heading, self.leg_hdg)) > 30:
            self.err *= 0.6
            self.leg_hdg = p.heading
        self.err = max(0.07, self.err * 0.997)
        self.g = clamp(self.g + self.rng.gauss(0, 0.08), -1, 1)
        if t < self.fix_until:
            er, eb = r, b
        else:
            k = 0.4 if self.is_torp else 1.0
            er = r * (1 + self.err * self.g * k)
            eb = b + self.rng.gauss(0, 0.8 + 6 / max(se, 1))
        self.brg = eb % 360
        self.rng_est = max(0.05, er)
        h = math.radians(self.brg)
        self.est = (p.x + self.rng_est * math.sin(h), p.y + self.rng_est * math.cos(h))

    def fix(self, p, t):
        self.fix_until = t + 60
        self.err = min(self.err, 0.1)
        self.observe(p, t, max(self.se, 10))


# ------------------------------------------------------------------- world


class World:
    def __init__(self, seed, patrol, loadout, log):
        self.rng = rng = random.Random(seed)
        self.t = 0.0
        self.patrol = patrol
        self.log = log
        self.env = Environment(rng)
        self.terrain = Terrain(rng)
        self.entities: list[Entity] = []
        self.tracks: dict[int, Track] = {}
        self.track_n = 0
        self.torp_n = 0
        self.weap_n = 0
        self.events: list[tuple] = []
        self.charges: list[list] = []
        self.deafen_until = -1.0
        self.selected: Track | None = None
        self.over = None
        self.score_items: list[tuple[str, int]] = []
        self.sensor_acc = 0.0
        self.p_mask = 0.0
        self.p_canyon = False
        self.p_bottom = False
        self.floor_here = 9999.0
        self.shoal_ahead = False
        self._rl: dict[str, float] = {}

        start, self.exfil = self._endpoints()
        hdg = bearing(start[0], start[1], *self.exfil)
        depth = min(400.0, self.terrain.floor(*start) - 150)
        self.player = Player(start[0], start[1], depth, hdg, rng, loadout)
        self.player.ord_depth = depth
        self.entities.append(self.player)
        self.floor_here = self.terrain.floor(self.player.x, self.player.y)
        self._populate()

    # ------------------------------------------------------- generation
    def _rot(self, x, y, k):
        c = MAP / 2
        for _ in range(k):
            x, y = c + (y - c), c - (x - c)
        return x, y

    def _endpoints(self):
        rng = self.rng
        k = rng.randint(0, 3)
        best = None
        for _ in range(60):
            s = self._rot(rng.uniform(3, 6), rng.uniform(8, 32), k)
            f = self.terrain.floor(*s)
            if best is None or f > best[1]:
                best = (s, f)
            if f > 620:
                break
        e = self._rot(rng.uniform(34, 37), rng.uniform(8, 32), k)
        self._axis_k = k
        return best[0], e

    def _away(self, mind=4.0):
        p = self.player
        while True:
            x, y = self.rng.uniform(2, MAP - 2), self.rng.uniform(2, MAP - 2)
            if math.hypot(x - p.x, y - p.y) > mind:
                return x, y

    def _populate(self):
        rng, P = self.rng, self.patrol
        lane = rng.uniform(0, 180)
        for _ in range(rng.randint(3, 5)):
            x, y = self._away(5)
            self.entities.append(Merchant(x, y, lane + rng.choice([0, 180]) + rng.uniform(-15, 15), rng))
        for _ in range(rng.randint(1, 3)):
            self.entities.append(Trawler(*self._away(4), rng.uniform(0, 360), rng))
        for _ in range(rng.randint(2, 4)):
            self.entities.append(Whale(*self._away(3), rng))
        sx, sy, ex, ey = self.player.x, self.player.y, *self.exfil
        n_w = 1 + (P >= 2) + (P >= 4)
        n_s = (1 if P >= 2 or rng.random() < 0.5 else 0) + (P >= 3) + (P >= 5)
        for _ in range(n_w):
            f = rng.uniform(0.35, 0.75)
            area = (sx + (ex - sx) * f + rng.uniform(-4, 4), sy + (ey - sy) * f + rng.uniform(-4, 4),
                    rng.uniform(6, 9))
            vds = self.env.layer_depth + 40 if rng.random() < min(0.6, 0.15 * P) else None
            a = rng.uniform(0, 6.283)
            self.entities.append(Warship(area[0] + 3 * math.sin(a), area[1] + 3 * math.cos(a),
                                         rng.uniform(0, 360), rng, area, vds))
        for _ in range(n_s):
            f = rng.uniform(0.5, 0.9)
            area = (sx + (ex - sx) * f, sy + (ey - sy) * f, rng.uniform(4, 7))
            L = self.env.layer_depth
            d = rng.choice([max(50, L - 70), L + 90])
            self.entities.append(Sub(area[0], area[1], d, rng.uniform(0, 360), rng, area))
        mil = n_w + n_s
        self.need = min(mil, 1 + (P >= 2) + (P >= 4))

    def respawn_merchant(self, m):
        m.alive = False
        rng = self.rng
        edge = rng.randint(0, 3)
        u = rng.uniform(4, MAP - 4)
        x, y = [(u, -4), (MAP + 4, u), (u, MAP + 4), (-4, u)][edge]
        h = bearing(x, y, MAP / 2 + rng.uniform(-10, 10), MAP / 2 + rng.uniform(-10, 10))
        self.entities.append(Merchant(x, y, h, rng))

    # ----------------------------------------------------------- utility
    def ambient(self):
        return self.env.ambient + (20.0 if self.t < self.deafen_until else 0.0)

    def say(self, text, lvl="i", key=None, every=0.0):
        if key is not None:
            if self.t - self._rl.get(key, -1e9) < every:
                return
            self._rl[key] = self.t
        self.log(text, lvl)

    def event(self, *ev):
        self.events.append(ev)

    def hostiles(self):
        return [e for e in self.entities if e.alive and isinstance(e, Hostile)]

    def track_for(self, e):
        return self.tracks.get(e.uid)

    def label(self, e):
        tr = self.tracks.get(e.uid)
        return tr.sid if tr else "unknown contact"

    def find_track(self, sid):
        sid = sid.upper()
        for tr in self.tracks.values():
            if tr.sid == sid:
                return tr
        return None

    def add_score(self, label, pts):
        self.score_items.append((label, pts))

    # --------------------------------------------------------------- step
    def step(self, dt):
        if self.over:
            return
        n = max(1, math.ceil(dt / SUBSTEP))
        h = dt / n
        for _ in range(n):
            self.substep(h)
            if self.over:
                break

    def substep(self, dt):
        self.t += dt
        for e in list(self.entities):
            if e.alive:
                e.update(self, dt)
        self._player_physics()
        self._charges()
        self.sensor_acc += dt
        while self.sensor_acc >= 1.0 and not self.over:
            self.sensor_acc -= 1.0
            self.sensor_tick()
        self.entities = [e for e in self.entities if e.alive or e is self.player]

    def _player_physics(self):
        p = self.player
        fl = self.terrain.floor(p.x, p.y)
        self.floor_here = fl
        if p.depth > fl - 4:
            dmg = 6 + p.speed * 1.5
            p.hull -= dmg
            p.depth = fl - 12
            p.ord_depth = min(p.ord_depth, fl - 25)
            p.speed *= 0.3
            self.say(f"COLLISION! We've struck the bottom at {fl:.0f} m. Hull -{dmg:.0f}%.", "r")
            self.event("hit")
        if p.depth > p.crush_depth:
            p.hull = 0
            self.over = ("dead", f"Hull imploded at {p.depth:.0f} m — crush depth exceeded.")
            return
        if p.hull <= 0:
            self.over = ("dead", "Pressure hull breached. Lost with all hands.")
            return
        if math.hypot(self.exfil[0] - p.x, self.exfil[1] - p.y) < 1.0:
            self.over = ("exfil", "Reached the exfiltration point.")

    # -------------------------------------------------------- sensor tick
    def sensor_tick(self):
        p = self.player
        self._player_systems()
        self._player_env()
        self._player_sensing()
        for e in list(self.entities):
            if e.alive and e is not p:
                e.think(self)
        self._intel()
        self._stress()

    def _player_systems(self):
        p = self.player
        for i, tb in enumerate(p.tubes):
            if tb["timer"] > 0:
                tb["timer"] -= 1
                if tb["timer"] <= 0:
                    tb["timer"] = 0
                    self.say(f"TORPEDO ROOM: Tube {i + 1} loaded, {tb['load']}, ready in all respects.", "c")
        if p.snorkel and p.depth > 20.5:
            if p.depth > 25:
                self.say("Snorkel mast is below the surface — head valve shut. Come to 18 m to snorkel.",
                         "y", "snkdepth", 60)
        drain = (0.8 if p.silent else 1.5) + (2.5 if p.scrub else 0) + 0.035 * p.speed ** 2 + 0.3
        if p.snorkel_active:
            if p.speed > 8:
                self.say("Snorkel: speed too high, mast vibration — slow to 8 kt or less.", "y", "snkspd", 60)
            else:
                drain -= 30
        if p.battery > 0 or drain < 0:
            p.battery = clamp(p.battery - drain / 3600, 0, 100)
            if p.battery <= 0:
                self.say("BATTERY FLAT. Main motor and scrubbers have no power!", "r")
                p.scrub = False
                self.event("alarm")
        if p.snorkel_active:
            p.co2 = max(0.3, p.co2 - 3.0 / 3600)
            p.o2 = min(20.9, p.o2 + 3.0 / 3600)
        elif p.scrub:
            p.co2 = max(0.5, p.co2 - 0.6 / 3600)
            p.o2 = min(20.5, p.o2 + 0.4 / 3600)
        else:
            p.co2 += 0.45 / 3600
            p.o2 -= 0.5 / 3600
        if p.co2 > 3.0:
            self.say(f"Crew showing CO2 symptoms ({p.co2:.1f}%) — headaches, slow reactions.", "y", "co2", 300)
        if p.co2 >= 7.0 or p.o2 <= 13.0:
            self.over = ("dead", "The crew succumbed to the atmosphere. Silent forever.")

    def _player_env(self):
        p = self.player
        fl = self.floor_here
        rim = self.terrain.rim(p.x, p.y)
        self.p_canyon = p.depth > rim + 20 and fl > p.depth
        self.p_bottom = fl - p.depth < 40
        self.p_mask = (10.0 if self.p_canyon else 0.0) + (4.0 if self.p_bottom else 0.0)
        h = math.radians(p.heading)
        self.shoal_ahead = False
        for d in (0.3, 0.6, 1.0):
            f = self.terrain.floor(p.x + d * math.sin(h), p.y + d * math.cos(h))
            if f < p.depth + 25:
                self.shoal_ahead = True
                break
        if self.shoal_ahead:
            self.say("OBSTACLE SONAR: rising bottom ahead at our depth!", "y", "shoal", 30)
            self.event("warn", "shoal")

    def _player_sensing(self):
        p = self.player
        nl = psum(self.ambient(), p.self_noise())
        for e in self.entities:
            if e is p or not e.alive or (e.side == "own"):
                continue
            r = dist(p, e)
            b = bearing(p.x, p.y, e.x, e.y)
            layer = self.env.layer_loss(p.depth, e.depth)
            bonus = 0.0
            if p.sonar == "NARROW":
                bonus = 6.0 if abs(angdiff(b, p.narrow_brg)) <= 20 else -10.0
            se = (e.sl(self.t) - tl(r) - layer - self.p_mask * 0.6 - nl + DI_OWN + bonus - DT_DET
                  + self.rng.gauss(0, 2))
            rel = angdiff(b, p.heading)
            e.se_own, e.brg_own, e.r_own = se, b, r
            e.baffled = abs(rel) > 150
            det = se > 0 and not e.baffled
            tr = self.tracks.get(e.uid)
            if det:
                if tr is None:
                    e.pend += 1
                    if e.pend >= (1 if e.kind == "torpedo" else 2):
                        tr = self._new_track(e)
                        tr.observe(p, self.t, se)
                        self._announce(tr)
                else:
                    was_lost = tr.lost
                    tr.observe(p, self.t, se)
                    if was_lost:
                        self.say(f"SONAR: {tr.sid} reacquired, bearing {tr.brg:03.0f}.", "g")
                    if e.speed - tr.spd_ref > 5:
                        self.say(f"SONAR: {tr.sid} blade rate increasing — contact is speeding up.", "y")
                        tr.spd_ref = e.speed
                    elif tr.spd_ref - e.speed > 5:
                        tr.spd_ref = e.speed
            else:
                e.pend = 0
                if tr is not None:
                    tr.held = False
        for tr in self.tracks.values():
            if not tr.held and not tr.lost and (not tr.ent.alive or self.t - tr.last > 30):
                tr.lost = True
                if not tr.ent.alive and tr.is_torp:
                    continue
                if tr.ent.alive:
                    self.say(f"SONAR: lost contact {tr.sid}, last bearing {tr.brg:03.0f}.", "d")

    def _new_track(self, e):
        if e.kind == "torpedo":
            self.torp_n += 1
            sid = f"T{self.torp_n}"
        else:
            self.track_n += 1
            sid = f"S{self.track_n}"
        tr = Track(sid, e, self.t, self.rng, self.player)
        self.tracks[e.uid] = tr
        return tr

    def _announce(self, tr):
        if tr.is_torp:
            self.say(f"TORPEDO IN THE WATER! Bearing {tr.brg:03.0f}, designated {tr.sid}!", "R")
            self.event("torpedo")
        else:
            self.say(f"SONAR: new contact {tr.sid}, bearing {tr.brg:03.0f}, signal {tr.se:+.0f} dB.", "g")
            self.event("contact")

    def _intel(self):
        tr = self.selected
        if tr is None or tr.recorded or not tr.held or tr.se < 3 or tr.ent.kind not in INTEL_PTS:
            return
        tr.intel += (min(tr.se, 15) - 1) * 0.12
        if tr.intel >= 100:
            tr.intel = 100
            tr.recorded = True
            k = tr.ent.kind
            self.add_score(f"Signature {tr.sid} ({CLASS_NAMES[k]})", INTEL_PTS[k])
            self.say(f"INTEL: signature of {tr.sid} recorded. Analysts confirm: {CLASS_NAMES[k]}.", "c")
            if tr.cls is not None and tr.cls != k:
                self.say(f"INTEL: our classification of {tr.sid} as {CLASS_NAMES[tr.cls]} was WRONG.", "y")
            self.event("intel")

    def _stress(self):
        p = self.player
        if p.depth > p.test_depth:
            frac = (p.depth - p.test_depth) / max(1.0, p.crush_depth - p.test_depth)
            if self.rng.random() < 0.05 + 0.3 * frac:
                p.hull -= self.rng.uniform(0.5, 2.5)
                self.say(f"The hull groans — frames working at {p.depth:.0f} m. Below test depth!", "y",
                         "creak", 12)
                self.event("creak")
        elif p.depth > 280 and self.rng.random() < 0.004:
            self.say(self.rng.choice([
                "A long metallic creak runs aft along the hull.",
                "Pinging and popping from the outer casing as it compresses.",
                "Condensation drips in the control room. Someone coughs.",
            ]), "d")
            self.event("creak")

    # ------------------------------------------------------------ actions
    def player_ping(self):
        p = self.player
        p.battery = max(0.0, p.battery - 1.0)
        nl = psum(self.ambient(), p.self_noise())
        found = 0
        for e in self.entities:
            if e is p or not e.alive or e.side == "own":
                continue
            r = dist(p, e)
            layer = self.env.layer_loss(p.depth, e.depth)
            se = 220 - 2 * (tl(r) + layer) + e.ts - nl + DI_OWN - 10
            if se > 0:
                tr = self.tracks.get(e.uid) or self._new_track(e)
                tr.se = max(tr.se, se)
                tr.fix(p, self.t)
                found += 1
        for h in self.hostiles():
            r = dist(h, p)
            layer = self.env.layer_loss(h.sensor_depth(self), p.depth)
            if 215 - tl(r) - layer - h.nl(self) + h.di - DT_DET > 0:
                h.register(self, 0.2 + r * 0.02, precise=True, strength=5)
        self.event("ping")
        return found

    def enemy_ping(self, h):
        p = self.player
        r = dist(h, p)
        sd = h.sensor_depth(self)
        layer = self.env.layer_loss(sd, p.depth)
        se = 225 - 2 * (tl(r) + layer) + p.ts - h.nl(self) + h.di - 10 - self.p_mask + self.rng.gauss(0, 3)
        if se > 0:
            h.register(self, 0.05 + 0.01 * r, precise=True, strength=3)
        heard = 225 - tl(r) - layer - psum(self.ambient(), p.self_noise()) + DI_OWN - DT_DET
        if heard > 0:
            b = bearing(p.x, p.y, h.x, h.y)
            tr = self.tracks.get(h.uid)
            if tr is None:
                tr = self._new_track(h)
                tr.observe(p, self.t, 6)
                tr.held = False
            name = tr.sid
            self.say(f"ACTIVE SONAR INTERCEPT bearing {b:03.0f} ({name}) — 3.5 kHz hull sonar. "
                     f"We are being pinged!", "R" if se > 0 else "y", f"ping{h.uid}", 45)
            self.event("enemy_ping", b, se > 0)

    def drop_charges(self, h, n):
        kx, ky = h.known
        for _ in range(n):
            d = clamp(h.known_depth + self.rng.gauss(0, 20), 15, 600)
            self.charges.append([kx + self.rng.gauss(0, 0.06), ky + self.rng.gauss(0, 0.06), d,
                                 self.t + d / 5.0 + self.rng.uniform(0, 3)])
        b = bearing(self.player.x, self.player.y, h.x, h.y)
        self.say(f"SPLASHES bearing {b:03.0f} — depth charges in the water!", "R", "dcsplash", 20)
        self.event("alarm")

    def _charges(self):
        p = self.player
        for c in list(self.charges):
            if self.t < c[3]:
                continue
            self.charges.remove(c)
            d3 = math.hypot(math.hypot(c[0] - p.x, c[1] - p.y) * NM_M, c[2] - p.depth)
            self.deafen_until = self.t + 6
            self.event("explosion", d3)
            if d3 < 180:
                dmg = 40 * (1 - d3 / 180) ** 1.5
                p.hull -= dmg
                self.say(f"DEPTH CHARGE detonation {d3:.0f} m away! Hull -{dmg:.0f}%.", "R")
            else:
                self.say(f"Depth charge detonation, {d3:.0f} m — no damage.", "y", "dcfar", 4)

    def asroc(self, h):
        kx, ky = h.known
        x, y = kx + self.rng.gauss(0, 0.25), ky + self.rng.gauss(0, 0.25)
        tp = Torpedo("hostile", x, y, 20, self.rng.uniform(0, 360), 40, 0, 6.0,
                     clamp(h.known_depth, 30, 500), self.rng, "ASW")
        self.entities.append(tp)
        b = bearing(self.player.x, self.player.y, x, y)
        self.say(f"SPLASH bearing {b:03.0f} — air-dropped torpedo entering the water!", "R")
        self.event("alarm")

    def hostile_launch(self, h, hdg, enable):
        tp = Torpedo("hostile", h.x, h.y, h.depth, hdg, 40, enable, 8.0,
                     clamp(h.known_depth, 30, 500), self.rng, "HT")
        self.entities.append(tp)
        p = self.player
        if h.sl(self.t) + 25 - tl(dist(h, p)) - self.env.layer_loss(h.depth, p.depth) - 60 > 0:
            b = bearing(p.x, p.y, h.x, h.y)
            self.say(f"TRANSIENT bearing {b:03.0f} — tube doors, possible torpedo launch!", "R")
            self.event("alarm")

    def spawn_decoy(self, owner, side, hdg):
        d = Decoy(side, owner.x, owner.y, owner.depth, hdg, self.rng)
        self.entities.append(d)
        return d

    def torpedo_lock_change(self, tp, old, new):
        if tp.side == "own":
            if new is not None:
                tr = self.tracks.get(new.uid)
                what = tr.sid if tr else ("a decoy" if new.kind == "decoy" else "an unknown target")
                self.say(f"WEAPON {tp.name}: seeker has acquired {what}. Homing.", "c")
            elif old is not None:
                self.say(f"WEAPON {tp.name}: lost acquisition, re-attacking.", "y")
        else:
            p = self.player
            if new is p:
                tr = self.tracks.get(tp.uid)
                nm = tr.sid if tr else "Torpedo"
                self.say(f"{nm} HAS ACQUIRED US! Pinging is steady — evade!", "R", f"acq{tp.uid}", 15)
                self.event("torpedo")
            elif old is p:
                self.say("Enemy torpedo seeker has lost us...", "g", f"lost{tp.uid}", 15)

    def torpedo_spent(self, tp):
        if tp.side == "own":
            self.say(f"WEAPON {tp.name}: fuel exhausted, weapon shut down.", "d")
        else:
            tr = self.tracks.get(tp.uid)
            if tr:
                self.say(f"SONAR: {tr.sid} screws have stopped — enemy torpedo ran out of fuel.", "g")

    def detonate(self, tp, target):
        tp.alive = False
        p = self.player
        self.event("explosion", math.hypot(dist(p, tp) * NM_M, tp.depth - p.depth))
        self.deafen_until = self.t + 8
        if target.kind == "decoy":
            target.alive = False
            if tp.side == "hostile":
                self.say("Detonation astern — the enemy torpedo took our decoy!", "g")
            else:
                self.say(f"WEAPON {tp.name} detonated on an enemy decoy.", "y")
            return
        if target is p:
            dmg = self.rng.uniform(55, 85)
            p.hull -= dmg
            self.say(f"TORPEDO HIT! Flooding! Hull -{dmg:.0f}%!", "R")
            self.event("hit")
            return
        target.alive = False
        target.sunk = True
        tr = self.tracks.get(target.uid)
        name = tr.sid if tr else "an unidentified contact"
        b = bearing(p.x, p.y, target.x, target.y)
        self.say(f"Explosion bearing {b:03.0f}! Breaking-up noises from {name}.", "c")
        k = target.kind
        pts = KILL_PTS.get(k, 0)
        self.add_score(f"Sank {name} ({CLASS_NAMES.get(k, k)})", pts)
        if pts < 0:
            self.say(f"INTEL: {name} was a {CLASS_NAMES[k]}. That's a non-combatant — "
                     f"every warship in the area is coming.", "R")
            for h in self.hostiles():
                h.set_datum(self, p.x, p.y, 1.5, p.depth, 6)
        else:
            for h in self.hostiles():
                if dist(h, target) < 15:
                    h.set_datum(self, p.x, p.y, 3.0, h.guess_depth(self), 3)
        if tr:
            tr.sunk = True

    # ---------------------------------------------------- player weapons
    def fire_tube(self, n, arg):
        p = self.player
        tb = p.tubes[n - 1]
        if tb["load"] is None:
            return f"Tube {n} is empty.", "y"
        if tb["timer"] > 0:
            return f"Tube {n} is still loading ({tb['timer']:.0f} s).", "y"
        hdg, enable, sdepth, tag = p.heading, 1.0, p.depth, ""
        if arg:
            if arg[0] == "S":
                tr = self.find_track(arg)
                if tr is None:
                    return f"No such contact {arg}.", "y"
                hdg = tr.brg
                enable = max(0.3, tr.rng_est - 1.2)
                if tr.cls in SURFACE_KINDS:
                    sdepth = 8.0
                tag = f" at {tr.sid}"
            else:
                try:
                    hdg = float(arg) % 360
                except ValueError:
                    return f"Bad bearing '{arg}'.", "y"
        if tb["load"] == "TORP":
            self.weap_n += 1
            tp = Torpedo("own", p.x, p.y, p.depth, hdg, 45, enable, 14.0, sdepth, self.rng, f"W{self.weap_n}")
            self.entities.append(tp)
            for h in self.hostiles():
                if dist(h, p) < 8:
                    h.register(self, 1.0, strength=2)
            msg = f"Tube {n} fired{tag}! {tp.name} running hot, bearing {hdg:03.0f}, enable at {enable:.1f} nm."
        else:
            dh = hdg if arg else (p.heading + 180) % 360
            self.spawn_decoy(p, "own", dh)
            msg = f"Tube {n}: decoy away, drifting {dh:03.0f}. Noisemaker active."
        tb["load"] = None
        self.event("launch")
        return msg, "c"
