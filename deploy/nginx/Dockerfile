FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:1.30.6-alpine
COPY --from=web /web/dist/ /usr/share/nginx/html/
COPY deploy/nginx/default.conf.template /etc/nginx/templates/default.conf.template
