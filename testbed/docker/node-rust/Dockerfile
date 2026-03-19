FROM rust:alpine AS builder

RUN apk add --no-cache musl-dev pkgconfig openssl-dev openssl-libs-static

WORKDIR /app

# Copy manifest first for layer caching
COPY testbed/docker/node-rust/Cargo.toml ./
# Create dummy src to cache dependencies
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release 2>/dev/null || true

# Copy real source and rebuild
COPY testbed/docker/node-rust/src/main.rs src/main.rs
RUN touch src/main.rs && cargo build --release

FROM alpine:3.20

RUN apk add --no-cache iproute2 bash iptables

COPY --from=builder /app/target/release/autonat-node-rust /usr/local/bin/autonat-node
COPY testbed/docker/node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
