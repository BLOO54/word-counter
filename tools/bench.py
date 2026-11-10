"""Accuracy benchmark: how many of the words get caught - normal, fast, quiet and noisy speech.

Used by the GitHub build:
  python tools/bench.py lines                 -> prints the sentences (a computer voice reads them)
  python tools/bench.py prepare DIR           -> turns DIR/raw_<rate>_<n>.wav into test files + manifest
Then:  WordCounter.exe --bench DIR REPORT --model better
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wordcounter import matching  # noqa: E402

WORDS = ["connor", "blade", "bro", "no way", "bruh"]
LINES = [
    "Connor, get over here right now.",
    "Bro, that was actually insane.",
    "No way he just did that, bruh.",
    "Blade is camping in the corner again.",
    "Yo Connor, pass me the ammo, bro.",
    "Bruh, I can't even see anything.",
    "Blade, watch your left, no way he's alone.",
    "Are you serious right now, Connor?",
    "Bro I'm literally out of shields.",
    "That's crazy, no way bro.",
    "Turn left at the corner and push.",          # trick sentence: "corner" must NOT count as Connor
    "Who's got the last kill? Blade or Connor?",
]
RATES = ("normal", "fast")
KINDS = {             # name: (voice volume, background noise level)
    "clear": (1.0, 0.0),
    "quiet": (0.07, 0.0),
    "noisy": (1.0, 0.03),
    "quiet+noisy": (0.1, 0.006),
}
RATE_HZ = 16000


def read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE_HZ and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def write(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE_HZ)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def game_noise(n: int, level: float, seed: int) -> np.ndarray:
    """Rumbly background like a game: brown-ish noise plus a low hum."""
    if level <= 0:
        return np.zeros(n, np.float32)
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n).astype(np.float32)
    brown = np.cumsum(white)
    brown -= np.convolve(brown, np.ones(400) / 400, mode="same")       # keep it from drifting
    brown /= np.std(brown) + 1e-9
    hum = np.sin(2 * np.pi * 95 * np.arange(n) / RATE_HZ)
    mix = 0.8 * brown + 0.2 * white / (np.std(white) + 1e-9) + 0.4 * hum
    return (mix / (np.sqrt(np.mean(mix * mix)) + 1e-9) * level).astype(np.float32)


def expected() -> dict[str, int]:
    return {w: sum(matching.count(line, w, similar=False) for line in LINES) for w in WORDS}


def prepare(folder: Path) -> None:
    manifest = {"words": WORDS, "expected_per_file": expected(), "files": []}
    gap = np.zeros(int(RATE_HZ * 1.2), np.float32)
    for rate in RATES:
        clips = [read(folder / f"raw_{rate}_{i}.wav") for i in range(len(LINES))]
        speech = np.concatenate([np.concatenate([c, gap]) for c in clips])
        for k, (vol, noise) in KINDS.items():
            audio = speech * vol + game_noise(len(speech), noise, seed=len(manifest["files"]))
            name = f"{rate}_{k}.wav"
            write(folder / name, audio)
            manifest["files"].append({"file": name, "rate": rate, "kind": k})
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("prepared", len(manifest["files"]), "test files; expected per file:", manifest["expected_per_file"])


if __name__ == "__main__":
    if sys.argv[1] == "lines":
        print("\n".join(LINES))
    elif sys.argv[1] == "prepare":
        prepare(Path(sys.argv[2]))
