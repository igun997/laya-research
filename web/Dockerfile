# syntax=docker/dockerfile:1

# --- build ---------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app

# `npm ci` is lockfile-exact: the committed package-lock.json is the contract.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# --- production dependencies ---------------------------------------------
# adapter-node bundles the server, so the runtime tree is intentionally thin.
FROM node:22-alpine AS prod-deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && mkdir -p node_modules

# --- runtime -------------------------------------------------------------
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000 \
    HOST=0.0.0.0

COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=builder /app/build ./build
COPY --from=builder /app/package.json ./package.json

# alpine ships an unprivileged `node` user (uid/gid 1000); never run as root.
USER node

EXPOSE 3000

CMD ["node", "build"]
