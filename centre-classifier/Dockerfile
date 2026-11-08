FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY centre_interval.py interval_legend.py pipeline.py overlay.py ./

ENTRYPOINT ["python"]
CMD ["pipeline.py"]
