# TODO: change from run to up, use .env
docker compose run certbot \
   certonly \
     --non-interactive \
     --agree-tos \
     --email $1 \
     --preferred-challenges dns \
     --authenticator dns-duckdns \
     --dns-duckdns-credentials /duckdns.ini \
     --dns-duckdns-propagation-seconds 120  \
     -d "*.$2" \
     -d "$2"