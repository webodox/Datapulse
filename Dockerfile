# Run DataPulse without installing Python or anything else on your machine:
#   docker build -t datapulse .
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/work datapulse check data/orders.csv
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src

WORKDIR /work
ENTRYPOINT ["datapulse"]
CMD ["--help"]
