# syntax=docker/dockerfile:1
#
# GENAIIDP-arabic-ocr-hook container image: PaddleOCR (Arabic boxes), plus the
# r5ocr package that also calls Textract (English/Latin boxes) and Bedrock
# (full-page transcription, unchanged from the IDP's own Bedrock OCR).
# Pin by digest for reproducible builds (see README.md, Security notes):
#   FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12@sha256:<digest>
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12

# PADDLE_PDX_CACHE_HOME must be set for BOTH the build (so the RUN step below
# bakes the two Arabic PP-OCRv5 models into the image) and the runtime (Lambda
# mounts everything under LAMBDA_TASK_ROOT, i.e. /var/task, read-only, so
# PaddleX must never try to write there or under its default ~/.paddlex).
#
# HOME=/tmp and XDG_CACHE_HOME=/tmp/.cache: Lambda's execution environment is
# read-only everywhere except /tmp — this applies whether or not the function
# actually runs as non-root, and the IDP's container Lambdas do (a `HOME`
# resolving to `/`, the default when no passwd entry exists for the running
# uid, is itself unwritable there). Various libraries pulled in by paddleocr
# (fontconfig, matplotlib, huggingface_hub/modelscope's `~/.cache`) fall back
# to `$HOME`/`$XDG_CACHE_HOME` for scratch state that is not the baked model
# cache above; pointing both at /tmp before anything imports them means none
# of that ever targets an unwritable path. Verified empirically with
# `docker run --read-only --tmpfs /tmp --user 993:990` — see README.md's
# local container test.
ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex \
    PYTHONDONTWRITEBYTECODE=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

# mesa-libGL: PaddleOCR imports cv2 (opencv-python-headless), which still
# dlopens libGL at import time on this base image. libgomp: paddlepaddle's
# OpenMP runtime.
RUN dnf install -y mesa-libGL libgomp && dnf clean all

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake the Arabic detector + recognizer models at build time (no runtime
# downloads; /var/task and $HOME are read-only on Lambda). This is the only
# network access PaddleX needs; PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK avoids a
# version-check call on every cold start once the models are local.
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='ar', text_detection_model_name='PP-OCRv5_mobile_det', text_recognition_model_name='arabic_PP-OCRv5_mobile_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, device='cpu', enable_mkldnn=False)"

COPY index.py ${LAMBDA_TASK_ROOT}/
COPY r5ocr ${LAMBDA_TASK_ROOT}/r5ocr

CMD ["index.lambda_handler"]
