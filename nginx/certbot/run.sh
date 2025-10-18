# Run: nginx/certbot/run.sh <email> <dns adderss>
docker compose exec certbot certbot \
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