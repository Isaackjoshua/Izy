"""The mascot's three postures, as vector art.

SVG rather than image files: it stays crisp on any scale factor, it is one
text file instead of six PNGs at different densities, and the whole thing is
still self-contained with nothing to ship alongside the code.

SPEC.md Feature 4 governs everything here, and it is a design of restraint:

  * **No idle animation. None.** Nothing in these drawings moves. The only
    motion the mascot ever makes is a ~400ms cross-fade between two of these
    postures, and that only when its state genuinely changes.
  * State is communicated by *static posture* and a subtle colour shift.
    Posture does most of the work, so the state is still legible to someone who
    cannot distinguish the colours.
  * No faces pulled into expressions of judgement. Off-task is not
    disappointment — the drifting posture looks *away*, it does not frown.

48×56 at 1x, drawn on a 48×56 viewBox so one SVG unit is one logical pixel.
"""
from __future__ import annotations

#: Body colours per state, matched to the retrospective's palette so the two
#: surfaces of the product agree: blue for on task, orange for drifting, gray
#: for asleep.
BODY = {
    "neutral": "#3d7fd6",
    "soft-alert": "#e0803c",
    "asleep": "#7c7a74",
}
SHADE = {
    "neutral": "#2f66b0",
    "soft-alert": "#bd6529",
    "asleep": "#62615c",
}

STATES = ("neutral", "soft-alert", "asleep")

_HEAD = ("<svg xmlns='http://www.w3.org/2000/svg' width='48' height='56' "
         "viewBox='0 0 48 56'>")


def _shell(state: str, body_d: str, face: str, lean: float = 0.0) -> str:
    """Body silhouette plus face, optionally leaning a couple of degrees.

    The lean is what makes the postures readable at 48px without any change in
    colour — it survives being seen out of the corner of an eye, which is the
    only way this thing is ever actually looked at.
    """
    transform = (f" transform='rotate({lean} 24 40)'" if lean else "")
    return (f"{_HEAD}<g{transform}>"
            f"<path d='{body_d}' fill='{BODY[state]}'/>"
            f"<path d='{body_d}' fill='none' stroke='{SHADE[state]}' "
            f"stroke-width='1.5' stroke-opacity='0.55'/>"
            f"{face}</g></svg>")


#: Upright, slightly tall: awake and settled.
_BODY_UPRIGHT = ("M24 4 C34 4 40 11 40 21 L40 40 C40 48 33 52 24 52 "
                 "C15 52 8 48 8 40 L8 21 C8 11 14 4 24 4 Z")

#: Same silhouette, a touch squatter: at rest.
_BODY_RESTING = ("M24 10 C34 10 40 16 40 25 L40 41 C40 48 33 52 24 52 "
                 "C15 52 8 48 8 41 L8 25 C8 16 14 10 24 10 Z")


PUPIL_R = 1.7


def _eyes(cx_offset: float = 0.0, cy: float = 24.0, r: float = 3.4) -> str:
    """The whites barely move; the pupils travel within them.

    The pupil offset is clamped so a pupil can never leave its own eye — the
    first attempt at a stronger glance pushed them clean outside the whites,
    which is unmistakable at 4x and just looks like noise at 48px.
    """
    limit = r - PUPIL_R - 0.15
    pupil = max(-limit, min(limit, cx_offset))
    white = cx_offset * 0.3
    return (f"<circle cx='{17.5 + white:.2f}' cy='{cy}' r='{r}' fill='#ffffff'/>"
            f"<circle cx='{30.5 + white:.2f}' cy='{cy}' r='{r}' fill='#ffffff'/>"
            f"<circle cx='{17.5 + white + pupil:.2f}' cy='{cy}' r='{PUPIL_R}' fill='#1b1b1b'/>"
            f"<circle cx='{30.5 + white + pupil:.2f}' cy='{cy}' r='{PUPIL_R}' fill='#1b1b1b'/>")


def _closed_eyes(cy: float = 30.0) -> str:
    return (f"<path d='M14 {cy} q3.5 3 7 0' fill='none' stroke='#ffffff' "
            f"stroke-width='1.8' stroke-linecap='round' stroke-opacity='0.85'/>"
            f"<path d='M27 {cy} q3.5 3 7 0' fill='none' stroke='#ffffff' "
            f"stroke-width='1.8' stroke-linecap='round' stroke-opacity='0.85'/>")


def neutral() -> str:
    """Session running, on task. Upright, looking straight ahead."""
    return _shell("neutral", _BODY_UPRIGHT, _eyes())


def soft_alert() -> str:
    """Drifting. Leaning away and looking off to the side.

    Deliberately not a frown: the posture reads as 'attention has gone
    elsewhere', which is what actually happened, rather than as disapproval.
    """
    return _shell("soft-alert", _BODY_UPRIGHT, _eyes(cx_offset=3.4), lean=-9)


def asleep() -> str:
    """No session. Settled, eyes closed — present but not watching."""
    return _shell("asleep", _BODY_RESTING, _closed_eyes())


SVG = {"neutral": neutral, "soft-alert": soft_alert, "asleep": asleep}


def svg_for(state: str) -> str:
    return SVG.get(state, asleep)()
