FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl unzip ca-certificates fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
RUN deno --version
RUN pip install --no-cache-dir -U "yt-dlp[default]" pillow
COPY yt_resolver.py /app/yt_resolver.py
CMD ["sh", "-c", "pip install -U 'yt-dlp[default]' && exec python /app/yt_resolver.py"]
