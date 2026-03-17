FROM node:22-alpine AS builder

WORKDIR /app

# Copy manifest first for layer caching
COPY testbed/docker/node-js/package.json testbed/docker/node-js/tsconfig.json ./
RUN npm install

# Copy source and compile
COPY testbed/docker/node-js/src/ src/
RUN npx tsc

FROM node:22-alpine

RUN apk add --no-cache iproute2 bash iptables

WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./

# Wrapper script that node invokes
RUN printf '#!/bin/bash\nexec node /app/dist/index.js "$@"\n' > /usr/local/bin/autonat-node && \
    chmod +x /usr/local/bin/autonat-node

COPY testbed/docker/node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
