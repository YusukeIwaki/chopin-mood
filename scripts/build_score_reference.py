#!/usr/bin/env python3
"""Rebuild the pinned Op. 39 symbolic reference without a notation library.

This deliberately narrow **kern reader implements rational rhythm, explicit
spine operations and pitch spelling used by the registered source. It does not
unroll repeats, infer missing rhythm, or silently repair unsupported notation.
All-null data records do not advance musical time. Tied continuation segments
retain occupancy but are not attacks. Grace notes occupy zero score time.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

PROFILE_ID = "chopin-scherzo3-coda-v1"
SOURCE_COMMIT = "95dfb105c1669c72d10b04088566154f12d3dc1c"
NOTE_COLUMNS = ["quarter_n", "quarter_d", "duration_n", "duration_d", "midi",
                "staff", "measure", "beat_n", "beat_d", "attack", "line"]


def rational(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def duration(token: str) -> Fraction:
    if "q" in token:
        return Fraction(0)
    m = re.search(r"(\d+)(?:%(\d+))?(\.*)", token)
    if m is None:
        raise ValueError(f"missing explicit reciprocal rhythm: {token!r}")
    reciprocal, numerator, dots = m.groups()
    if int(reciprocal):
        base = Fraction(4 * int(numerator or 1), int(reciprocal))
    elif numerator:
        raise ValueError("zero reciprocal with ratio is unsupported")
    else:
        base = Fraction(4 * 2 ** len(reciprocal))
    return base * sum((Fraction(1, 2 ** i) for i in range(len(dots) + 1)), Fraction())


def pitch(token: str) -> int | None:
    if "r" in token:
        return None
    m = re.search(r"([a-g]+|[A-G]+)([#n-]*)", token)
    if m is None:
        raise ValueError(f"missing pitch or rest: {token!r}")
    letters, accidental = m.groups()
    if len(set(letters)) != 1 or ("n" in accidental and ("#" in accidental or "-" in accidental)):
        raise ValueError(f"unsupported pitch spelling: {token!r}")
    octave = 4 + len(letters) - 1 if letters.islower() else 4 - len(letters)
    midi = (octave + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5,
                               "G": 7, "A": 9, "B": 11}[letters[0].upper()]
    midi += accidental.count("#") - accidental.count("-")
    if not 0 <= midi <= 127:
        raise ValueError(f"pitch outside MIDI range: {token!r}")
    return midi


@dataclass
class _Spine:
    kind: str
    staff: int | None = None
    remaining: Fraction = Fraction(0)


def parse_kern(text: str, *, source: str = "memory.krn") -> dict:
    """Parse explicit **kern spines; return stable compact, auditable JSON."""
    spines: list[_Spine] = []
    now = Fraction()
    measure = 0
    measures: dict[int, Fraction] = {}
    notes = []
    terminated = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("!!"):
            continue
        fields = line.split("\t")
        if not spines:
            if terminated:
                if not line.startswith("!"):
                    raise ValueError(f"line {lineno}: multiple segments are unsupported")
                continue
            if all(x.startswith("**") for x in fields):
                spines = [_Spine(x) for x in fields]
                continue
            if line.startswith("!"):
                continue
            raise ValueError(f"line {lineno}: expected exclusive spine interpretations")
        if len(fields) != len(spines):
            raise ValueError(f"line {lineno}: {len(fields)} columns for {len(spines)} spines")
        if line.startswith("!"):
            if not all(x.startswith("!") for x in fields):
                raise ValueError(f"line {lineno}: mixed comment/data record")
            continue
        if line.startswith("*"):
            if not all(x.startswith("*") for x in fields):
                raise ValueError(f"line {lineno}: mixed interpretation/data record")
            for spine, field in zip(spines, fields):
                if field.startswith("**"):
                    spine.kind = field
                elif field.startswith("*staff"):
                    if not re.fullmatch(r"\*staff\d+", field):
                        raise ValueError(f"line {lineno}: unsupported staff interpretation")
                    spine.staff = int(field[6:])
            exchanges = [i for i, x in enumerate(fields) if x == "*x"]
            if len(exchanges) % 2:
                raise ValueError(f"line {lineno}: unpaired spine exchange")
            for a, b in zip(exchanges[::2], exchanges[1::2]):
                spines[a], spines[b] = spines[b], spines[a]
            new = []
            i = 0
            while i < len(spines):
                operation = fields[i]
                if operation == "*v":
                    end = i + 1
                    while end < len(fields) and fields[end] == "*v":
                        end += 1
                    group = spines[i:end]
                    if len(group) < 2 or len({(s.kind, s.staff, s.remaining) for s in group}) != 1:
                        raise ValueError(f"line {lineno}: incompatible spine merge")
                    new.append(copy.copy(group[0]))
                    i = end
                    continue
                if operation != "*-":
                    new.append(copy.copy(spines[i]))
                if operation == "*^":
                    new.append(copy.copy(spines[i]))
                elif operation == "*+":
                    new.append(_Spine(""))
                i += 1
            spines = new
            if not spines:
                terminated = True
            continue
        if line.startswith("="):
            if not all(x.startswith("=") for x in fields):
                raise ValueError(f"line {lineno}: mixed barline/data record")
            numbers = {int(m[1]) for x in fields if (m := re.match(r"=(\d+)", x))}
            if len(numbers) > 1:
                raise ValueError(f"line {lineno}: inconsistent measure numbers")
            if numbers:
                measure = numbers.pop()
                if measure in measures:
                    raise ValueError(f"line {lineno}: repeated measure number")
                measures[measure] = now
            continue
        active = [(s, x) for s, x in zip(spines, fields) if s.kind == "**kern" and x != "."]
        if not active:
            # Humdrum null records, including all-null after pedal changes,
            # encode no elapsed time. Never decrement sustained voice rhythm.
            continue
        is_grace = False
        for spine, field in active:
            tokens = field.split()
            # Humdrum's spine clock uses the first chord subtoken. Preserve
            # each explicitly written note duration (TR m210 has 4e- 8B- 4E-)
            # instead of rewriting the shorter inner note as a quarter.
            lengths = [duration(t) for t in tokens]
            length = lengths[0]
            is_grace |= length == 0
            if length > 0:
                if spine.remaining:
                    raise ValueError(f"line {lineno}: note before previous duration completed")
                spine.remaining = length
            if measure not in measures:
                raise ValueError(f"line {lineno}: unnumbered measure is unsupported")
            beat = now - measures[measure] + 1
            for token, note_length in zip(tokens, lengths):
                midi = pitch(token)
                if midi is None:
                    continue
                if spine.staff is None:
                    raise ValueError(f"line {lineno}: sounding note has no staff")
                notes.append([now.numerator, now.denominator, note_length.numerator,
                              note_length.denominator, midi, spine.staff, measure,
                              beat.numerator, beat.denominator,
                              not ("_" in token or "]" in token), lineno])
        if not is_grace:
            live = [s.remaining for s in spines if s.kind == "**kern" and s.remaining > 0]
            if live:
                elapsed = min(live)
                now += elapsed
                for spine in spines:
                    if spine.kind == "**kern":
                        spine.remaining = max(Fraction(), spine.remaining - elapsed)
    if spines or not terminated:
        raise ValueError("unterminated spines")
    return {"schema_version": 1, "source": source, "note_columns": NOTE_COLUMNS,
            "quarter_length": rational(now),
            "measures": [[m, *rational(q)] for m, q in measures.items()], "notes": notes}


def serialized(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def build(bundle_root: Path) -> dict:
    """Rebuild only the supplied local, pinned sources; no network access."""
    files = {}
    for name in ("039-1-TR.krn", "039-1-BH.krn"):
        source = bundle_root / name
        value = parse_kern(source.read_text(), source=name)
        if value["quarter_length"] != [1947, 1] or len(value["measures"]) != 649:
            raise ValueError(f"unexpected Op. 39 length in {name}")
        if any(Fraction(n, d) != (m - 1) * 3 for m, n, d in value["measures"]):
            raise ValueError(f"unexpected 3/4 bar grid in {name}")
        target = bundle_root / (name + ".notes.json")
        target.write_bytes(serialized(value))
        for p in (source, target):
            files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    for name in ("LICENSE.txt", "ATTRIBUTION.md"):
        files[name] = hashlib.sha256((bundle_root / name).read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1, "profile_id": PROFILE_ID,
        "reference_id": "chopin-op39-v1", "primary": "039-1-TR.krn.notes.json",
        "corroborating": "039-1-BH.krn.notes.json",
        "source": {"repository": "pl-wnifc/humdrum-chopin-first-editions",
                   "commit": SOURCE_COMMIT,
                   "url": "https://github.com/pl-wnifc/humdrum-chopin-first-editions/tree/" + SOURCE_COMMIT,
                   "path": "kern/039-1-TR.krn"},
        "license": {"id": "CC-BY-4.0", "url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution_path": "ATTRIBUTION.md", "commercial_adaptation": True},
        "derivation": {"script": "scripts/build_score_reference.py", "parser_version": 1,
                       "repeat_policy": "not unrolled; explicit written score order",
                       "grace_policy": "zero duration; not in chroma occupancy",
                       "tie_policy": "segments sustain occupancy; continuation is not an attack",
                       "chord_rhythm_policy": "first subtoken advances spine; explicit per-note durations preserved",
                       "pitch_policy": "explicit kern sounding-pitch spelling, including naturals; octave lines are display only; no key inference"},
        "landmarks": [
            {"id": "major_arrival", "score_quarter": [1623, 1], "measure": 542, "beat": [1, 1],
             "meaning": "C-sharp major arrival; does not label the entire coda as major"},
            {"id": "both_hands_octaves", "score_quarter": [1699, 1], "measure": 567, "beat": [2, 1],
             "meaning": "first strict both-hands octave attack; preceding beat has RH octaves and LH single B-sharp"},
            {"id": "fast_coda", "score_quarter": [1716, 1], "measure": 573, "beat": [1, 1],
             "meaning": "fast coda; new LH C-sharp2 while RH G-sharp3 continues a tie"}],
        "files": files,
    }
    (bundle_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "assets/score/chopin-op39-v1")
    args = parser.parse_args()
    build(args.bundle_root)


if __name__ == "__main__":
    main()
