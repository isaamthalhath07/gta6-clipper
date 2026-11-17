"""Word-level transcription with faster-whisper; one model shared by all channels."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list[Word]


class Transcriber:
    def __init__(self, cfg):
        self.cfg = cfg
        self._model = None
        self._failed = False
        self._pool = ThreadPoolExecutor(max_workers=max(1, cfg.workers), thread_name_prefix="whisper")

    def _load(self):
        if self._model is not None or self._failed:
            return self._model
        try:
            from faster_whisper import WhisperModel

            device, compute = self.cfg.device, self.cfg.compute_type
            if device == "auto":
                try:
                    import ctranslate2

                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:
                    device = "cpu"
            if compute == "auto":
                compute = "float16" if device == "cuda" else "int8"
            self._model = WhisperModel(self.cfg.model, device=device, compute_type=compute, num_workers=max(1, self.cfg.workers))
            log.info("whisper %s loaded on %s/%s", self.cfg.model, device, compute)
        except Exception as e:  # model download blocked, no CUDA libs, etc.
            self._failed = True
            log.warning("transcription disabled: %s", e)
        return self._model

    def _transcribe_sync(self, audio: np.ndarray) -> list[Sentence]:
        model = self._load()
        if model is None or len(audio) == 0:
            return []
        segments, _info = model.transcribe(
            audio, language="en" if self.cfg.model.endswith(".en") else None, word_timestamps=True,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300}, beam_size=5,
            condition_on_previous_text=False,
        )
        out = []
        for seg in segments:
            words = [Word(w.start, w.end, w.word.strip()) for w in (seg.words or []) if w.word.strip()]
            if words:
                out.append(Sentence(words[0].start, words[-1].end, seg.text.strip(), words))
        return out

    async def transcribe(self, audio: np.ndarray) -> list[Sentence]:
        if not self.cfg.enabled:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._transcribe_sync, audio)

    def warmup(self) -> None:
        if self.cfg.enabled:
            self._pool.submit(self._load)
