"""Entry point: python -m subzero [--seed N] [--sound] [--no-bell]"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import time

from .game import Game
from .sound import Sound
from .ui import UI

FPS = 20


def run(scr, args):
    curses.curs_set(0)
    scr.nodelay(True)
    scr.keypad(True)
    try:
        curses.set_escdelay(25)
    except AttributeError:
        pass
    sound = Sound(pcm=args.sound, bell=not args.no_bell, beep_fn=curses.beep)
    game = Game(seed=args.seed, sound=sound)
    ui = UI(scr)
    last = time.monotonic()
    try:
        while not game.quit:
            now = time.monotonic()
            dt = min(0.25, now - last)
            last = now
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                if ch == curses.KEY_RESIZE:
                    continue
                game.handle_key(ch)
            game.update(dt)
            ui.render(game, game.real)
            spent = time.monotonic() - now
            time.sleep(max(0.0, 1.0 / FPS - spent))
    finally:
        sound.close()


def main():
    ap = argparse.ArgumentParser(prog="subzero", description="Cold War acoustic submarine roguelike.")
    ap.add_argument("--seed", type=int, default=None, help="ocean seed (same seed = same run)")
    ap.add_argument("--sound", action="store_true", help="synthesized PCM sound via paplay/pw-play/aplay")
    ap.add_argument("--no-bell", action="store_true", help="disable terminal bell alerts")
    args = ap.parse_args()
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run, args)


if __name__ == "__main__":
    main()
