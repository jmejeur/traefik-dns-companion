FROM python:3.14-alpine
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY companion.py /companion.py
COPY backends/ /backends/
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,time; s=os.stat('/tmp/heartbeat'); sys.exit(0 if time.time()-s.st_mtime<600 else 1)"
CMD ["python", "-u", "/companion.py"]
