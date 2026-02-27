FROM golang:1.24-alpine AS builder

WORKDIR /app

# Copy patched go-libp2p first (referenced by replace directive in go.mod)
COPY go-libp2p-patched/ /app/go-libp2p-patched/

# Copy go.mod/go.sum and adjust replace directive for Docker context
COPY go.mod go.sum ./
RUN go mod edit -replace github.com/libp2p/go-libp2p=./go-libp2p-patched
RUN go mod download

COPY main.go mock_server.go ./
RUN CGO_ENABLED=0 go build -o /autonat-node .

FROM alpine:3.20

# iproute2 needed for setting default gateway in client containers
RUN apk add --no-cache iproute2 bash

COPY --from=builder /autonat-node /usr/local/bin/autonat-node
COPY docker/node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
