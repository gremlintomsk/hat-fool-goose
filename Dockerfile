FROM nginx:1.27.0-alpine

RUN apk add --no-cache python3

COPY nginx-docker.conf /etc/nginx/conf.d/default.conf
COPY api.py /opt/slg/api.py
COPY index.html main.js config.js /usr/share/nginx/html/
COPY vendor /usr/share/nginx/html/vendor
COPY img /usr/share/nginx/html/img

ENV SLG_DB=/data/leaderboard.db

EXPOSE 80
CMD ["sh", "-c", "python3 /opt/slg/api.py & exec nginx -g 'daemon off;'"]
