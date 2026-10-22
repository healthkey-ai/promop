# Reproducible, non-interactive ARTEMIS runner.  This is deliberately not the
# upstream RStudio convenience image: it has no exposed web UI and is intended
# to run one reviewed cohort job at a time.
FROM rocker/r-ver:4.4.3

ARG ARTEMIS_REF=242b5a24864b85a44c62d95a98cbaa2d16c55539

ENV DEBIAN_FRONTEND=noninteractive \
    ARTEMIS_PYTHON=/usr/local/bin/python3 \
    DATABASECONNECTOR_JAR_FOLDER=/opt/hades/jdbc_drivers \
    R_LIBS_USER=/opt/artemis/R/library

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git default-jdk \
      software-properties-common \
      build-essential libcurl4-openssl-dev libgit2-dev libssl-dev libxml2-dev \
      libfontconfig1-dev libfreetype6-dev libfribidi-dev libharfbuzz-dev \
      libpng-dev libjpeg-dev libtiff-dev zlib1g-dev \
    && add-apt-repository --yes ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3.12 python3.12-dev python3.12-venv \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/* \
    && R CMD javareconf \
    && curl -fsSL https://bootstrap.pypa.io/get-pip.py | python3 \
    && python3 -m pip install --no-cache-dir --break-system-packages \
      'numpy==2.2.0' 'pandas==2.2.3' 'cython==3.0.11' 'tqdm==4.67.1'

RUN R -q -e "install.packages(c('jsonlite', 'remotes', 'rJava'), repos='https://cloud.r-project.org')" \
    && R -q -e "remotes::install_github('OHDSI/DatabaseConnector', upgrade='never')" \
    && mkdir -p "${DATABASECONNECTOR_JAR_FOLDER}" "${R_LIBS_USER}" \
    && R -q -e "DatabaseConnector::downloadJdbcDrivers('postgresql')"

RUN git clone https://github.com/OHDSI/ARTEMIS.git /opt/artemis/source \
    && cd /opt/artemis/source \
    && git checkout --detach "${ARTEMIS_REF}" \
    && test "$(git rev-parse HEAD)" = "${ARTEMIS_REF}" \
    && R -q -e "remotes::install_deps('/opt/artemis/source', dependencies=c('Depends', 'Imports'), upgrade='never')" \
    && R -q -e "remotes::install_local('/opt/artemis/source', dependencies=FALSE, upgrade='never')" \
    && R -q -e "stopifnot(as.character(utils::packageVersion('ARTEMIS')) == '1.6.0')"

COPY run_artemis.R /opt/artemis/run_artemis.R
COPY validate-config.sh /usr/local/bin/validate-artemis-config
RUN chmod 0555 /opt/artemis/run_artemis.R /usr/local/bin/validate-artemis-config \
    && useradd --create-home --uid 10001 artemis \
    && mkdir -p /work/input /work/output \
    && chown -R artemis:artemis /work

USER artemis
WORKDIR /work
ENTRYPOINT ["Rscript", "/opt/artemis/run_artemis.R"]
