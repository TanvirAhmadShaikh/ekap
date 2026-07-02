FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests==2.32.3
COPY setup.py .
CMD ["python", "setup.py"]
