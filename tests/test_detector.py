import random

import pytest

from clipper.chat import ChatMessage
from clipper.config import DetectorCfg
from clipper.detector import ChannelDetector, MessageScorer

FUNNY = ["KEKW", "OMEGALUL", "LMAOOOO", "💀💀💀", "CLIP IT", "ICANT"]
NORMAL = ["hello", "what is he doing", "lol", "nice car", "chat is this real"]


def simulate(base_rate: float, *, spike_at=None, spam_at=None, seed=1, secs=600):
    random.seed(seed)
    d = ChannelDetector("ch", DetectorCfg(), 0)
    users = [f"u{i}" for i in range(3000)]
    spikes = []
    for sec in range(secs):
        rate, texts = base_rate * (1 + 0.5 * random.random()), NORMAL
        if spike_at is not None and spike_at <= sec < spike_at + 12:
            rate, texts = base_rate * 6 + 8, FUNNY
        if spam_at is not None and spam_at <= sec < spam_at + 40:
            for _ in range(30):
                d.add(ChatMessage("ch", "spammer", "KEKW KEKW KEKW", sec + random.random()))
        for _ in range(max(0, int(round(random.gauss(rate, rate ** 0.5))))):
            d.add(ChatMessage("ch", random.choice(users), random.choice(texts), sec + random.random()))
        spikes += d.tick(sec + 1.01)
    return spikes


@pytest.mark.parametrize("rate", [0.8, 6, 60])
def test_detects_spike_at_any_channel_size(rate):
    spikes = simulate(rate, spike_at=300)
    assert len(spikes) == 1
    s = spikes[0]
    assert 296 <= s.onset <= 302
    assert s.event == s.onset - DetectorCfg().reaction_lag_s
    assert s.mood == "funny"


@pytest.mark.parametrize("rate", [0.8, 6, 60])
def test_quiet_chat_has_no_spikes(rate):
    assert simulate(rate) == []


def test_single_spammer_cannot_trigger():
    assert simulate(0.8, spam_at=300) == []


def test_scorer_weights_hype_and_clip_requests():
    sc = MessageScorer(DetectorCfg().hype_tokens)
    plain, _ = sc.score(ChatMessage("c", "u", "hello there", 0))
    funny, moods = sc.score(ChatMessage("c", "u", "OMEGALUL", 0))
    clip, cm = sc.score(ChatMessage("c", "u", "clip it", 0))
    q, qm = sc.score(ChatMessage("c", "u", "????????", 0))
    assert plain < funny < clip
    assert moods["funny"] > 0 and cm["clip"] > 0 and qm["shock"] > 0
