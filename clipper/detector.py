"""Chat spike detection.

Per channel, per second, we compute a *hype-weighted* message rate in which each chatter
counts at most `per_user_cap` (spammers and copy-pasta bots can't fake a spike). A slow EWMA
gives the channel's baseline and spread; a spike starts when the short-window rate is both
statistically (z-score) and practically (ratio) above baseline with enough distinct chatters.
Baseline adapts per channel, so a 500-viewer and a 100k-viewer stream use the same settings.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from .chat import ChatMessage

_TOKEN_RE = re.compile(r"\?{3,}|[\w']+|[^\w\s]", re.UNICODE)
_REPEAT_RE = re.compile(r"(.)\1{3,}")


@dataclass
class Spike:
    channel: str
    onset: float  # wall time chat started rising
    peak: float  # wall time of the hottest second
    end: float
    event: float  # estimated on-stream moment = onset - reaction lag
    score: float  # peak short-window z-score
    ratio: float  # peak short-window rate / baseline
    unique_chatters: int
    mood: str
    top_messages: list[tuple[float, str, str]] = field(default_factory=list)  # (t, user, text)
    mood_counts: dict[str, float] = field(default_factory=dict)

    @property
    def strength(self) -> float:
        """Single number for ranking clips across channels."""
        return self.score * math.log2(1 + self.ratio) * math.log2(2 + self.unique_chatters)


class MessageScorer:
    def __init__(self, hype_tokens: dict[str, tuple[float, str]]):
        self.tokens = {k.lower(): v for k, v in hype_tokens.items()}

    def score(self, msg: ChatMessage) -> tuple[float, Counter]:
        text = msg.text.strip()
        low = text.lower()
        moods: Counter = Counter()
        best = 0.0
        seen: set[str] = set()
        for tok in _TOKEN_RE.findall(low):
            tok = _REPEAT_RE.sub(r"\1\1", tok) if not tok.startswith("??") else "???"
            for cand in (tok, tok.rstrip("!?.")):
                if cand in self.tokens and cand not in seen:
                    seen.add(cand)
                    w, mood = self.tokens[cand]
                    moods[mood] += w
                    best = max(best, w)
        compact = re.sub(r"\s+", "", low)
        for phrase in ("noway", "letsgo", "clipit", "holyshit"):
            if phrase in compact and phrase not in seen:
                w, mood = self.tokens.get(phrase, (1.6, "hype"))
                moods[mood] += w
                best = max(best, w)
        s = 1.0 + best + 0.3 * max(0, len(seen) - 1)
        if msg.emote_only:
            s += 0.4
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            s += 0.3  # ALL CAPS
        if _REPEAT_RE.search(text):
            s += 0.3  # LMAOOOO / ?????
        return s, moods


@dataclass
class _Bucket:
    sec: int
    users: dict[str, float] = field(default_factory=dict)
    count: int = 0
    moods: Counter = field(default_factory=Counter)
    samples: list[tuple[float, str, str, float]] = field(default_factory=list)

    @property
    def weighted(self) -> float:
        return sum(self.users.values())


class ChannelDetector:
    def __init__(self, channel: str, cfg, start_time: float):
        self.channel = channel
        self.cfg = cfg
        self.scorer = MessageScorer(cfg.hype_tokens)
        self.start = start_time
        self.buckets: deque[_Bucket] = deque(maxlen=120)
        self.mu = 0.0
        self.var = 0.0
        self._init = False
        self._n = 0
        self._alpha = 1 - math.exp(math.log(0.5) / max(cfg.baseline_half_life_s, 1.0))
        self._episode: dict | None = None
        self._last_end = -1e18
        self._last_sec: int | None = None

    # -- ingest -----------------------------------------------------------------------
    def add(self, msg: ChatMessage) -> None:
        sec = int(msg.t)
        b = self._bucket(sec)
        if b is None:
            return
        s, moods = self.scorer.score(msg)
        b.users[msg.user] = min(self.cfg.per_user_cap, b.users.get(msg.user, 0.0) + s)
        b.count += 1
        b.moods.update(moods)
        if len(b.samples) < 40:
            b.samples.append((msg.t, msg.user, msg.text[:200], s))

    def _bucket(self, sec: int) -> _Bucket | None:
        if not self.buckets or sec > self.buckets[-1].sec:
            last = self.buckets[-1].sec if self.buckets else sec - 1
            for s in range(max(last + 1, sec - 119), sec + 1):
                self.buckets.append(_Bucket(s))
            return self.buckets[-1]
        for b in reversed(self.buckets):  # late message for a recent second
            if b.sec == sec:
                return b
            if b.sec < sec:
                break
        return None

    # -- detection --------------------------------------------------------------------
    def tick(self, now: float) -> list[Spike]:
        """Call ~once per second. Processes every fully elapsed second, returns finished spikes."""
        self._bucket(int(now))  # make sure empty seconds exist
        out: list[Spike] = []
        done_sec = int(now) - 1  # current second is still filling
        start_sec = done_sec if self._last_sec is None else self._last_sec + 1
        for sec in range(start_sec, done_sec + 1):
            spike = self._process_second(sec)
            if spike:
                out.append(spike)
        self._last_sec = done_sec
        return out

    def _window(self, end_sec: int, n: int) -> list[_Bucket]:
        return [b for b in self.buckets if end_sec - n < b.sec <= end_sec]

    def _process_second(self, sec: int) -> Spike | None:
        cfg = self.cfg
        cur = next((b for b in reversed(self.buckets) if b.sec == sec), None)
        if cur is None:
            return None
        win = self._window(sec, cfg.short_window_s)
        short = sum(b.weighted for b in win) / max(len(win), 1)
        per_user: Counter = Counter()
        for b in win:
            per_user.update(b.users)
        uniq = len(per_user)
        top_share = (per_user.most_common(1)[0][1] / max(sum(per_user.values()), 1e-9)) if per_user else 1.0
        sd = math.sqrt(max(self.var, 0.0))
        # noise floor: Poisson-like spread of a window mean, inflated for chat burstiness
        floor = math.sqrt(max(self.mu, 0.3) / cfg.short_window_s) * 1.5
        z = (short - self.mu) / max(sd / math.sqrt(cfg.short_window_s) * 1.5, floor, 0.3)
        ratio = short / max(self.mu, 0.3)
        warm = sec - self.start >= cfg.warmup_s

        ep = self._episode
        if ep is None:
            if (
                warm and sec - self._last_end >= cfg.cooldown_s and z >= cfg.z_enter
                and ratio >= cfg.min_ratio and uniq >= cfg.min_unique_chatters
                and top_share <= cfg.max_user_share
            ):
                onset = self._find_onset(sec)
                self._episode = {"onset": onset, "peak": sec, "z": z, "ratio": ratio, "uniq": uniq, "start": sec}
            else:
                self._update_baseline(cur.weighted)
            return None

        if z > ep["z"]:
            ep.update(peak=sec, z=z, ratio=ratio)
        ep["uniq"] = max(ep["uniq"], uniq)
        if z < cfg.z_exit or sec - ep["start"] >= cfg.max_episode_s:
            self._episode = None
            self._last_end = sec
            return self._make_spike(ep, sec)
        return None

    def _update_baseline(self, x: float) -> None:
        if not self._init:
            self.mu, self.var, self._init = x, max(x, 1.0), True
            return
        # cumulative mean while young (unbiased warm-up), then a slow EWMA
        self._n += 1
        a = max(self._alpha, 1.0 / (self._n + 1))
        d = x - self.mu
        self.mu += a * d
        self.var = (1 - a) * (self.var + a * d * d)

    def _find_onset(self, trigger_sec: int) -> float:
        """Walk back to where the rise began (first second clearly above baseline)."""
        thr = self.mu + max(math.sqrt(max(self.var, 0.0)), 0.5)
        onset = trigger_sec
        for b in reversed(self._window(trigger_sec, self.cfg.short_window_s + 6)):
            if b.weighted > thr:
                onset = b.sec
            elif b.sec < onset - 1:
                break
        return float(onset)

    def _make_spike(self, ep: dict, end_sec: int) -> Spike:
        bks = [b for b in self.buckets if ep["onset"] - 1 <= b.sec <= end_sec]
        moods: Counter = Counter()
        samples = []
        for b in bks:
            moods.update(b.moods)
            samples += b.samples
        samples.sort(key=lambda s: -s[3])
        top = sorted(((t, u, txt) for t, u, txt, _ in samples[:25]), key=lambda s: s[0])
        clip_votes = moods.pop("clip", 0.0)
        mood = moods.most_common(1)[0][0] if moods else "hype"
        if clip_votes and not moods:
            mood = "hype"
        return Spike(
            channel=self.channel, onset=ep["onset"], peak=float(ep["peak"]), end=float(end_sec),
            event=ep["onset"] - self.cfg.reaction_lag_s, score=round(ep["z"], 2), ratio=round(ep["ratio"], 2),
            unique_chatters=ep["uniq"], mood=mood, top_messages=top,
            mood_counts={k: round(v, 1) for k, v in {**moods, "clip": clip_votes}.items()},
        )

    def stats(self) -> dict:
        win = self._window(self.buckets[-1].sec - 1, 10) if self.buckets else []
        return {
            "msgs_per_s": round(sum(b.count for b in win) / max(len(win), 1), 1),
            "baseline": round(self.mu, 2),
            "in_spike": self._episode is not None,
        }
