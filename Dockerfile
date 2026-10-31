# RaiseRadar: shows the report on port 8080 and refreshes it on a schedule.
#   docker run -p 8080:8080 -e OPENROUTER_API_KEY=sk-or-... -v raiseradar-data:/app/data ghcr.io/mrhello291/raiseradar
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as a normal user, not root.
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY raiseradar ./raiseradar
RUN pip install . && rm -rf build raiseradar *.egg-info      # run the installed copy, not the source folder

COPY config.toml ./config.toml
# The folders are created and handed to `app` before they become volumes, so a fresh volume is writable.
RUN mkdir -p data reports && chown -R app:app /app
USER app
VOLUME ["/app/data", "/app/reports"]

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3)"

ENTRYPOINT ["raiseradar"]
CMD ["serve", "--host", "0.0.0.0"]
