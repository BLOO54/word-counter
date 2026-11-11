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
    assert 0.9 < len(pieces[0]) / RATE < 2.2


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


def test_to_16k_mono():
    from wordcounter.audio import to_16k_mono
    stereo48 = np.ones((4800, 2), np.float32) * 0.5
    out = to_16k_mono(stereo48, 48000)
    assert out.shape == (1600,) and np.allclose(out, 0.5)
    out = to_16k_mono(np.zeros((4410, 2), np.float32), 44100)
    assert abs(len(out) - 1600) <= 1
    assert to_16k_mono(np.zeros((1600, 1), np.float32), 16000).shape == (1600,)


def test_similar_spellings():
    c = matching.count
    assert c("Conner said hi to Conor and Connor", "connor") == 3
    assert c("Connor's mic is broken", "connor") == 1
    assert c("go to the corner, it's a cannon", "connor") == 0
    assert c("bros being bros", "bro") == 2
    assert c("brother", "bro") == 0
    assert c("Conner", "connor", similar=False) == 0
    assert c("no wey bro", "no way") == 0          # short words must match exactly


def test_highlight_similar():
    text = "Hey Conner, Connor's here, corner bros"
    spans = matching.highlight_spans(text, ["connor", "bro"])
    assert [text[a:b] for a, b in spans] == ["Conner", "Connor's", "bros"]


def test_listener_has_every_source():
    from wordcounter.audio import Listener
    for name in ("run", "_run_mic", "_run_app", "_open"):
        assert callable(getattr(Listener, name, None)), name


def test_gpu_version_pick():
    from wordcounter import gpu
    assert gpu._version_key("12.4.5.8") > gpu._version_key("12.4.2.65")
    assert gpu._version_key("9.10.2.21") > gpu._version_key("9.9.0")
    assert gpu.has_nvidia_driver() in (True, False)


def test_milestones():
    from wordcounter import stats
    assert stats.crossed_milestone(48, 51, 25) == 50
    assert stats.crossed_milestone(9, 10, 10) == 10
    assert stats.crossed_milestone(10, 11, 10) is None
    assert stats.crossed_milestone(3, 99, 0) is None


def test_live_counter_never_double_counts():
    from wordcounter.stats import LiveCounter
    lc, words = LiveCounter(), ["bro", "pizza"]
    assert lc.update("mic", 1, "hey bro", False, words) == {"bro": 1}           # counted early
    assert lc.update("mic", 1, "hey bro how", False, words) == {}               # nothing new
    assert lc.update("mic", 1, "hey bro how are you bro", False, words) == {"bro": 1}
    assert lc.update("mic", 1, "Hey bro, how are you, bro?", True, words) == {}  # final agrees: no change
    assert lc.update("mic", 1, "hey bro bro bro", False, words) == {}           # late live check ignored


def test_live_counter_final_corrects():
    from wordcounter.stats import LiveCounter
    lc, words = LiveCounter(), ["connor"]
    assert lc.update("discord", 7, "is that conner", False, words) == {"connor": 1}
    assert lc.update("discord", 7, "is that a corner", True, words) == {"connor": -1}   # misheard early
    assert lc.update("discord", 8, "connor", True, words) == {"connor": 1}             # no live check: final only
    assert lc.update("mic", 7, "connor", True, words) == {"connor": 1}                 # other source, own piece


def test_segmenter_live_checks():
    pieces = []
    seg = Segmenter(on_piece=lambda pid, a, final: pieces.append((pid, len(a), final)), live=lambda: True)
    audio = np.concatenate([np.zeros(RATE, np.float32), _tone(2.5, 0.3), np.zeros(RATE, np.float32)])
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        seg.add(audio[i:i + FRAME])
    finals = [p for p in pieces if p[2]]
    lives = [p for p in pieces if not p[2]]
    assert len(finals) == 1 and len(lives) >= 2
    assert all(p[0] == finals[0][0] for p in lives)                 # all checks belong to the same sentence
    assert all(p[1] < finals[0][1] for p in lives)


def test_vram_text():
    from wordcounter import vram
    assert vram.model_vram_text("best", {}) == "~1.8 GB"
    assert vram.model_vram_text("best", {}, careful=True) == "~2.0 GB"
    assert vram.model_vram_text("max", {"max": 3_120_000_000}) == "3.1 GB (measured)"
    assert vram.card_memory() is None or len(vram.card_memory()) == 2


def test_every_source_is_known():
    from wordcounter.audio import APPS, Listener
    from wordcounter.config import SOURCE_PARTS, SOURCES
    assert set(SOURCES) == set(SOURCE_PARTS)
    for parts in SOURCE_PARTS.values():
        for kind in parts:
            assert kind in Listener.NAMES and (kind in APPS or kind in ("mic", "pc", "bot"))
    assert callable(getattr(Listener, "_run_app", None))


def test_sensitive_mode_hears_quiet_speech():
    quiet = np.concatenate([np.zeros(RATE, np.float32), _tone(1.0, 0.004), np.zeros(RATE, np.float32)])
    for sens, want in ((False, 0), (True, 1)):
        pieces = []
        seg = Segmenter(pieces.append, adaptive=False, sensitive=lambda s=sens: s)
        for i in range(0, len(quiet) - FRAME + 1, FRAME):
            seg.add(quiet[i:i + FRAME])
        assert len(pieces) == want


def test_level_up_quiet_speech():
    from wordcounter.recognizer import level_up
    quiet = _tone(1.0, 0.01)
    louder = level_up(quiet, [{"start": 0, "end": len(quiet)}])
    assert 4 < np.abs(louder).max() / np.abs(quiet).max() <= 8.01
    loud = _tone(1.0, 0.3)
    assert np.array_equal(level_up(loud, [{"start": 0, "end": len(loud)}]), loud)   # normal talk untouched


def test_bot_frames_and_pieces():
    import io
    import json
    import struct
    from wordcounter import discord_bot as db

    def fr(kind, payload):
        return bytes([kind]) + struct.pack("<I", len(payload)) + payload
    uid = b"1234"
    tone = (np.sin(np.arange(16000) / 5) * 8000).astype(np.int16).tobytes()     # 1 s of voice
    stream = io.BytesIO(fr(1, json.dumps({"t": "start", "id": "1234", "name": "Connor"}).encode())
                        + fr(2, bytes([len(uid)]) + uid + tone)
                        + fr(1, json.dumps({"t": "end", "id": "1234"}).encode()))
    pieces, statuses = [], []
    bl = db.BotListener("x", lambda s, a, pid, final: pieces.append((s, len(a), pid, final)),
                        lambda k, t: statuses.append(t), lambda k, v: None, live=lambda: True)
    for kind, payload in db.read_frames(stream):
        if kind == 1:
            bl._event(json.loads(payload))
        else:
            n = payload[0]
            bl._audio(payload[1:1 + n].decode(), payload[1 + n:])
    assert bl.names["1234"] == "Connor"
    finals = [p for p in pieces if p[3]]
    assert len(finals) == 1 and finals[0][0] == "bot:1234" and finals[0][1] == 16000
    assert all(p[2] == finals[0][2] for p in pieces)            # live checks belong to the same sentence


def test_bot_token_helpers():
    from wordcounter import discord_bot as db
    import base64
    token = base64.b64encode(b"112233445566778899").decode().rstrip("=") + ".abc.def"
    assert db.bot_id_from_token(token) == "112233445566778899"
    assert "client_id=112233445566778899" in db.invite_url("112233445566778899")
    assert db.unprotect(db.protect("secret-token")) == "secret-token"


def test_people_counts():
    from wordcounter import stats
    w = {"text": "bro", "count": 0}
    stats.add_person(w, "1", 3)
    stats.add_person(w, "2", 5)
    stats.add_person(w, "3", 1)
    stats.add_person(w, "4", 1)
    stats.add_person(w, "1", -1)                       # a live early count corrected
    short, full = stats.people_line(w, {"1": "Connor", "2": "Blade", "3": "tessi"})
    assert short == "Blade 5 · Connor 2 · tessi 1 · +1 more"
    assert full.splitlines()[-1] == "someone 1"
    stats.add_person(w, "2", -9)
    assert "2" not in w["by"]
    assert stats.people_line({"text": "x"}, {}) == ("", "")
