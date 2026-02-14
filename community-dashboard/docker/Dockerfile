# =============================================================================
# Stage 1: Build the strands-metrics Rust binary
# =============================================================================
FROM rust:1.88-alpine AS builder

RUN apk add --no-cache musl-dev pkgconfig openssl-dev openssl-libs-static

WORKDIR /build

# Copy the Rust project source
COPY strands-metrics/Cargo.toml strands-metrics/Cargo.lock* ./
COPY strands-metrics/src/ ./src/

# Build in release mode (statically linked against musl)
RUN cargo build --release

# =============================================================================
# Stage 2: Unified Grafana + metrics sync image
# =============================================================================
FROM grafana/grafana:latest

USER root

# Install ca-certificates and curl for downloading supercronic
RUN apk add --no-cache ca-certificates curl

# Install supercronic (cron replacement designed for containers)
ARG SUPERCRONIC_VERSION=v0.2.43
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64
RUN curl -fsSL "${SUPERCRONIC_URL}" -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

# Copy the compiled strands-metrics binary
COPY --from=builder /build/target/release/strands-metrics /usr/local/bin/strands-metrics

# Copy Grafana provisioning configs
COPY provisioning/datasources/ /etc/grafana/provisioning/datasources/
COPY provisioning/dashboards/ /etc/grafana/provisioning/dashboards/

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create the data directory for EFS / local mount
RUN mkdir -p /var/lib/grafana/data

# Bundle a pre-built metrics.db so the first boot doesn't need a long backfill.
# The entrypoint will copy this to the EFS volume if no DB exists yet.
# The wildcard pattern makes this a no-op if the file doesn't exist.
COPY docker/data/metrics.d[b] /seed/

# Grafana configuration via environment variables
ENV GF_INSTALL_PLUGINS=frser-sqlite-datasource
ENV GF_PLUGIN_FRSER_SQLITE_DATASOURCE_ALLOW_LOCAL_MODE=true
ENV GF_AUTH_ANONYMOUS_ENABLED=true
ENV GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
ENV GF_AUTH_BASIC_ENABLED=false
ENV GF_AUTH_DISABLE_LOGIN_FORM=true
ENV GF_USERS_ALLOW_SIGN_UP=false
ENV GF_SECURITY_ALLOW_EMBEDDING=true

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
