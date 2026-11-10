import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wordcounter import matching  # noqa: E402
from wordcounter.audio import FRAME, RATE, Segmenter  # noqa: E402


def test_whole_words_only():
    assert matching.count("Bro, my brother is a bro.", "bro") == 2
    assert matching.count("BRO!!! bro? bro...", "bro") == 3
    assert matching.count("brother", "bro") == 0


def test_phrases_and_apostrophes():
    assert matching.count("No way. no, way. No way!", "no way") == 3
    assert matching.count("That’s crazy, thats crazy", "that's crazy") == 2
    assert matching.count("anything", "") == 0


def test_highlight_spans():
    text = "Bro, no way bro"
    spans = matching.highlight_spans(text, ["bro", "no way"])
    assert [text[a:b] for a, b in spans] == ["Bro", "no way", "bro"]


def _tone(seconds, amp):
    t = np.arange(int(RATE * seconds)) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_segmenter_splits_on_pauses():
    pieces = []
    seg = Segmenter(pieces.append)
    audio = np.concatenate([np.zeros(RATE, np.float32), _tone(1.0, 0.3), np.zeros(RATE, np.float32),
                            _tone(0.8, 0.3), np.zeros(RATE, np.float32)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        seg.add(audio[i:i + FRAME])
    assert len(pieces) == 2
    assert 0.9 < len(pieces[0]) / RATE < 1.9


def test_segmenter_cuts_long_talking():
    pieces = []
    seg = Segmenter(pieces.append, adaptive=False)
    audio = np.concatenate([_tone(14, 0.3), np.zeros(RATE, np.float32)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        seg.add(audio[i:i + FRAME])
    assert len(pieces) >= 2
    assert all(len(p) / RATE <= 6.1 for p in pieces)
    assert abs(sum(len(p) for p in pieces) / RATE - 14) < 1.0    # nothing lost or doubled


def test_segmenter_ignores_silence():
    pieces = []
    seg = Segmenter(pieces.append)
    for _ in range(100):
        seg.add(np.zeros(FRAME, np.float32))
    assert pieces == []


from wordcounter import updater  # noqa: E402


def test_version_compare():
    assert updater.is_newer("v1.1.0", "1.0.0")
    assert updater.is_newer("1.10.0", "1.9.3")
    assert not updater.is_newer("v1.1.0", "1.1.0")
    assert not updater.is_newer("1.0.9", "1.1")


def test_check_latest_reads_github_answer():
    import io
    import json

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    answer = {"tag_name": "v9.0.0", "html_url": "https://example/rel", "body": "What's new: faster",
              "assets": [{"name": "WordCounter-Portable-9.0.0.zip", "browser_download_url": "z", "size": 1},
                         {"name": "WordCounter-Setup-9.0.0.exe", "browser_download_url": "https://x/s.exe",
                          "size": 123, "digest": "sha256:ABC"}]}
    info = updater.check_latest("1.1.0", opener=lambda req, timeout: Resp(json.dumps(answer).encode()))
    assert info.version == "9.0.0" and info.installer_name.endswith(".exe") and info.sha256 == "abc"
    assert updater.whats_new(info) == "faster"
    assert updater.check_latest("9.0.0", opener=lambda req, timeout: Resp(json.dumps(answer).encode())) is None
