# Run: nginx/certbot/run.sh <email> <dns address>
docker compose run --rm --entrypoint "" certbot certbot \
   certonly \
     --non-interactive \
     --agree-tos \
     --email $1 \
     --preferred-challenges dns \
     --authenticator dns-cloudflare \
     --dns-cloudflare-credentials /cloudflare.ini \
     --dns-cloudflare-propagation-seconds 30  \
     -d "*.$2" \
     -d "$2"