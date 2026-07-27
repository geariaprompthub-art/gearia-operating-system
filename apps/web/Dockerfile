FROM node:22-alpine AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

WORKDIR /app
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -S gearia && adduser -S gearia -G gearia
COPY --from=builder --chown=gearia:gearia /app/package.json ./package.json
COPY --from=builder --chown=gearia:gearia /app/package-lock.json ./package-lock.json
COPY --from=builder --chown=gearia:gearia /app/node_modules ./node_modules
COPY --from=builder --chown=gearia:gearia /app/.next ./.next

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD node -e "fetch('http://127.0.0.1:3000').then((response) => process.exit(response.ok ? 0 : 1)).catch(() => process.exit(1))"

USER gearia
CMD ["npm", "run", "start", "--", "-H", "0.0.0.0"]

FROM builder AS test
