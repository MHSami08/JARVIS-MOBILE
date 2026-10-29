#!/usr/bin/env python3
"""
Turn air_trace.csv into a short report for live testing of AirGesture Pro.

  python analyze_trace.py --mark       remember "now"  (run BEFORE each test)
  python analyze_trace.py              report on rows since the mark (or last 10 s)
  python analyze_trace.py --last 8     report on the last 8 seconds
"""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FLASH = {"LEFT CLICK!", "RIGHT CLICK!", "CALIBRATED", "CALIBRATION SKIPPED"}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                row = dict(wall=num(r["wall"]), t=num(r["t"]), mode=r["mode"], pose=r["pose"],
                           L=num(r["L"]), R=num(r["R"]), fingers=r["fingers"], palm=num(r["palm"]),
                           ax=num(r["ax"]), ay=num(r["ay"]), cx=num(r["cx"]), cy=num(r["cy"]),
                           pinch=int(num(r["pinch"]) or 0), drag=int(num(r["drag"]) or 0),
                           fps=num(r["fps"]) or 0.0, events=[e for e in (r["event"] or "").split("|") if e])
            except (KeyError, TypeError, AttributeError):
                continue                      # half-written last line
            if row["wall"] is not None and row["t"] is not None:
                rows.append(row)
    return rows


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return "no data"
    return f"n={len(v)} min={min(v):.2f} p10={pct(v,10):.2f} med={pct(v,50):.2f} p90={pct(v,90):.2f}"


def segments(rows, key):
    """Yield (start_idx, end_idx) of consecutive rows where key(row) is truthy."""
    start = None
    for i, r in enumerate(rows):
        on = key(r)
        if on and start is None:
            start = i
        if not on and start is not None:
            yield start, i - 1
            start = None
    if start is not None:
        yield start, len(rows) - 1


def report(rows, meta):
    out, hints = [], []
    t0 = rows[0]["t"]
    rel = lambda r: r["t"] - t0
    dur = max(rows[-1]["t"] - t0, 1e-6)
    fps = len(rows) / dur
    hand = [r for r in rows if r["mode"] not in ("NO HAND", "ACQUIRING", "MOVE CLOSER")]

    out.append(f"WINDOW  {dur:.1f}s, {len(rows)} frames, {fps:.0f} fps, hand seen {100*len(hand)/len(rows):.0f}% of time")
    if fps < 20:
        hints.append("LOW FPS (<20): lower cam_w/cam_h, set draw_skeleton=false, close other camera apps.")

    modes = Counter("(flash)" if r["mode"] in FLASH else r["mode"] for r in rows)
    out.append("MODES   " + ", ".join(f"{m} {100*c/len(rows):.0f}%" for m, c in modes.most_common(6)))

    # ---- pose flicker
    posed = [r for r in hand if r["pose"]]
    switches, short = 0, 0
    if posed:
        cur, begin = posed[0]["pose"], posed[0]["t"]
        for r in posed[1:]:
            if r["pose"] != cur:
                switches += 1
                short += (r["t"] - begin) < 0.3
                cur, begin = r["pose"], r["t"]
    out.append(f"POSE    {switches} switches, {short} shorter than 0.3s")
    if short >= 2:
        hints.append("POSE FLICKER: raise mode_confirm_frames (3->4) or rest_hold_seconds.")

    # ---- events
    ev = [(rel(r), e) for r in rows for e in r["events"]]
    names = Counter(e.split(":")[0] for _, e in ev)
    out.append("EVENTS  " + (", ".join(f"{k} x{v}" for k, v in names.items()) or "none"))
    for t, e in ev[:25]:
        out.append(f"          {t:6.2f}s  {e}")
    if len(ev) > 25:
        out.append(f"          ... {len(ev)-25} more")

    # ---- left pinch episodes
    eps = list(segments(rows, lambda r: r["pinch"] == 1))
    closed = meta.get("pinch_threshold_closed", 0.33)
    clicks_t = [t for t, e in ev if e == "click"]
    downs_t = [t for t, e in ev if e == "down"]
    used_clicks, missed, multi = set(), [], 0
    for a, b in eps:
        ta, tb = rows[a]["t"] - t0, rows[b]["t"] - t0
        inside = [t for t in clicks_t if ta - 0.2 <= t <= tb + 0.5]
        used_clicks.update(inside)
        dd = [t for t in downs_t if ta - 0.2 <= t <= tb + 0.5]
        minL = min((r["L"] for r in rows[a:b + 1] if r["L"] is not None), default=float("nan"))
        if not inside and not dd:
            missed.append((ta, tb - ta, minL))
        if len(inside) > 1:
            multi += 1
    false_clicks = [t for t in clicks_t if t not in used_clicks]
    out.append(f"PINCH   {len(eps)} left-pinch episodes, {len(clicks_t)} clicks, {len(downs_t)} drags, "
               f"{len(missed)} pinch with no action, {len(false_clicks)} click with no pinch, {multi} multi-click")
    for ta, d, m in missed[:5]:
        out.append(f"          no action: pinch at {ta:.2f}s lasted {d:.2f}s minL={m:.2f}")
    if false_clicks:
        hints.append("FALSE CLICKS: raise pinch_threshold_open gap (open - closed >= 0.12), pinch_confirm_frames=3, lower pinch_smooth.")
    if multi:
        hints.append("DOUBLE FIRE: pinch flicker. Raise pinch_threshold_open or pinch_confirm_frames.")
    if missed:
        lows = [m for _, _, m in missed if m == m]
        if lows and min(lows) > closed:
            hints.append(f"MISSED PINCH: ratio never went below pinch_threshold_closed ({closed:.2f}). Raise it or press C to recalibrate.")
        else:
            hints.append("PINCH TOO SHORT: tap held less than drag/confirm time. Try pinch_confirm_frames=1.")

    # ---- drags
    drags = [(rows[a]["t"] - t0, rows[b]["t"] - rows[a]["t"]) for a, b in segments(rows, lambda r: r["drag"] == 1)]
    if drags:
        out.append("DRAG    " + ", ".join(f"at {t:.1f}s for {d:.1f}s" for t, d in drags[:6]))

    # ---- right click
    rc_thr = meta.get("right_click_threshold", 0.32)
    rc = [t for t, e in ev if e == "right"]
    rsegs = [(a, b) for a, b in segments(rows, lambda r: r["R"] is not None and r["R"] < rc_thr and r["mode"] != "NO HAND")
             if b - a >= 2]
    rmiss = [a for a, b in rsegs if not any(rows[a]["t"] - t0 - 0.1 <= t <= rows[b]["t"] - t0 + 0.2 for t in rc)]
    out.append(f"RIGHT   {len(rc)} right clicks, {len(rsegs)} middle-pinch episodes below threshold {rc_thr:.2f}, "
               f"{len(rmiss)} without click")
    if rc and len(rc) > len(rsegs):
        hints.append("FALSE RIGHT CLICK: lower right_click_threshold or raise pinch_min_reach (0.4->0.5).")
    if rmiss:
        hints.append("RIGHT CLICK BLOCKED while pinching: probably pinch with left pinch active or neighbour finger closer. Check L vs R.")

    # ---- scroll
    sc = [(t, int(e.split(":")[1])) for t, e in ev if e.startswith("scroll:")]
    if sc:
        pos = sum(v for _, v in sc if v > 0)
        neg = sum(v for _, v in sc if v < 0)
        bad = sum(1 for r in rows for e in r["events"] if e.startswith("scroll:") and r["pose"] != "SCROLL")
        out.append(f"SCROLL  {len(sc)} wheel events, up total {pos}, down total {neg}, {bad} outside scroll pose")

    # ---- cursor
    mv = [r for r in rows if r["cx"] is not None and r["mode"] not in ("NO HAND",)]
    steps, still_steps, jumps = [], [], []
    for p, q in zip(mv, mv[1:]):
        if None in (p["ax"], q["ax"]) or q["t"] - p["t"] > 0.2 or p["mode"] in ("ACQUIRING", "NO HAND"):
            continue                          # first frame after hand appears: cursor teleports, normal
        st = float(np.hypot(q["cx"] - p["cx"], q["cy"] - p["cy"]))
        hand_move = float(np.hypot(q["ax"] - p["ax"], q["ay"] - p["ay"]))
        steps.append(st)
        if hand_move < 1.0 and q["pose"] == "MOVE" and q["mode"] in ("CURSOR MOVE", "AIM LOCK"):
            still_steps.append(st)
        if st > 120 and p["pose"] == q["pose"] == "MOVE":
            jumps.append((rel(q), st, q["mode"]))
    if still_steps and len(still_steps) >= 10:
        p95 = pct(still_steps, 95)
        out.append(f"STILL   hand almost still for {len(still_steps)} frames: cursor step mean {np.mean(still_steps):.1f}px, "
                   f"p95 {p95:.1f}px, max {max(still_steps):.0f}px")
        if p95 > 3:
            hints.append("CURSOR SHAKE while hand still: lower smooth_min_cutoff (1.2->0.8), raise deadzone_stabilizer_px (2.2->3.5), raise active_width (less gain).")
    out.append(f"JUMPS   {len(jumps)} cursor jumps over 120px in one frame" +
               ("".join(f"\n          {t:.2f}s {s:.0f}px in {m}" for t, s, m in jumps[:5])))
    if jumps:
        hints.append("CURSOR JUMPS: tracker glitch or fast flick. Lower max_jump_palms (1.8->1.4) or raise smooth_min_cutoff; check TRACK GLITCH share.")

    mrows = [r for r in rows if r["mode"] in ("CURSOR MOVE", "AIM LOCK", "DRAGGING", "(flash)") or r["mode"] in FLASH]
    if mrows:
        lock = sum(1 for r in mrows if r["mode"] == "AIM LOCK") / len(mrows)
        out.append(f"REACH   cursor x {min(r['cx'] for r in mrows):.0f}..{max(r['cx'] for r in mrows):.0f} of {meta.get('screen_w','?')}, "
                   f"y {min(r['cy'] for r in mrows):.0f}..{max(r['cy'] for r in mrows):.0f} of {meta.get('screen_h','?')}; AIM LOCK {100*lock:.0f}% of move time")
        if lock > 0.15:
            hints.append("CURSOR FROZEN too often (AIM LOCK >15%): lower freeze_threshold_left/right by 0.05 or press C to recalibrate.")

    # ---- ratios by pose
    for pose in ("MOVE", "SCROLL", "REST"):
        sub = [r for r in rows if r["pose"] == pose]
        if len(sub) >= 5:
            out.append(f"RATIOS  {pose}: L {stats([r['L'] for r in sub])} | R {stats([r['R'] for r in sub])}")
    out.append("LIMITS  click<{:.2f} release>{:.2f} freezeL<{:.2f} rightclick<{:.2f} freezeR<{:.2f}".format(
        closed, meta.get("pinch_threshold_open", 0), meta.get("freeze_threshold_left", 0),
        rc_thr, meta.get("freeze_threshold_right", 0)))
    glitch = sum(1 for r in rows if r["mode"] == "TRACK GLITCH") / len(rows)
    if glitch > 0.03:
        hints.append(f"TRACKER GLITCHES {100*glitch:.0f}% of frames: better light, plain background, hand 40-70cm from camera.")

    out.append("")
    out.append("HINTS   " + ("none - nothing wrong found by numbers." if not hints else ""))
    out += [f"  - {h}" for h in hints]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", type=Path, default=HERE / "air_trace.csv")
    ap.add_argument("--mark", action="store_true", help="remember now, then exit")
    ap.add_argument("--last", type=float, default=None, help="analyse the last N seconds")
    args = ap.parse_args()
    mark_file = args.trace.with_suffix(".mark")

    if args.mark:
        mark_file.write_text(str(time.time()))
        print("mark saved. Run the test now.")
        return
    if not args.trace.exists():
        sys.exit(f"No trace file: {args.trace}. Start the app with: python air_mouse.py --trace")
    rows = load(args.trace)
    if len(rows) < 5:
        sys.exit("Trace is empty or too short. Is air_mouse.py running with --trace?")

    if args.last is not None:
        cut = rows[-1]["wall"] - args.last
    elif mark_file.exists():
        cut = float(mark_file.read_text())
    else:
        cut = rows[-1]["wall"] - 10
    rows = [r for r in rows if r["wall"] >= cut]
    if len(rows) < 5:
        sys.exit("Not enough frames after the mark. Is the app still running?")
    meta_path = args.trace.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    print(report(rows, meta))


if __name__ == "__main__":
    main()
