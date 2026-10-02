# Runs on Oracle Cloud Ampere A1 (ARM64) or any x86_64 VM. CPU only, no GPU needed.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu-core curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Face detector model + caption font. If a download fails the bot still works
# (Haar-cascade face detection / DejaVu font), just less accurately / less stylish.
RUN mkdir -p assets/fonts \
 && (curl -fsSL -o assets/face_detection_yunet.onnx \
      https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
     && test "$(stat -c%s assets/face_detection_yunet.onnx)" -gt 100000 \
     || (echo "WARNING: YuNet download failed" && rm -f assets/face_detection_yunet.onnx)) \
 && (curl -fsSL -o assets/fonts/Montserrat-ExtraBold.ttf \
      https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf \
     || (echo "WARNING: font download failed" && rm -f assets/fonts/Montserrat-ExtraBold.ttf)) \
 && fc-cache -f

COPY shared shared
COPY clipper clipper
COPY rater rater

ENV DATA_DIR=/data \
    YUNET_MODEL=/app/assets/face_detection_yunet.onnx \
    FONTS_DIR=/app/assets/fonts \
    PYTHONUNBUFFERED=1
VOLUME /data
