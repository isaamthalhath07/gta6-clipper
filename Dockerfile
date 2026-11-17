# CPU image. For NVIDIA encoding/transcription, start from an nvidia/cuda runtime image instead
# and set edit.encoder: h264_nvenc, transcribe.device: cuda.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-inter fontconfig ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY clipper ./clipper
RUN pip install --no-cache-dir .

VOLUME ["/app/data"]
ENTRYPOINT ["clipper"]
CMD ["run"]
