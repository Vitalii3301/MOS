FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py /app/server.py
RUN mkdir -p /data
EXPOSE 8791
CMD ["python", "-B", "/app/server.py"]
