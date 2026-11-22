# Same base image the commits were labeled in, so sqlglot's tests behave the same.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY fetch_sqlglot.py /app/fetch_sqlglot.py
RUN python /app/fetch_sqlglot.py \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot
COPY run.py selector.py fakepool.py *.bin /app/
RUN python -m compileall -q /app && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/run.py"]
