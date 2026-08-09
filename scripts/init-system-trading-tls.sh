#!/bin/sh
set -eu

lan_ip="${1:-172.26.12.120}"
ca_dir="${2:-secrets/system-trading-ca}"
tls_dir="${3:-secrets/system-trading-tls}"

case "$lan_ip" in
  *[!0-9.]*|'') echo "Invalid IPv4 address" >&2; exit 2 ;;
esac

if [ -e "$ca_dir/ca.key" ] || [ -e "$tls_dir/server.key" ]; then
  echo "Refusing to overwrite existing TLS material" >&2
  exit 2
fi

mkdir -p "$ca_dir" "$tls_dir"
chmod 700 "$ca_dir" "$tls_dir"

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$ca_dir/ca.key"
openssl req -x509 -new -sha256 -days 3650 -key "$ca_dir/ca.key" \
  -subj "/CN=Trading Bamboo Private CA/O=System Trading" -out "$ca_dir/ca.crt"

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$tls_dir/server.key"
openssl req -new -sha256 -key "$tls_dir/server.key" \
  -subj "/CN=$lan_ip/O=System Trading" -out "$tls_dir/server.csr"

extension_file=$(mktemp)
trap 'rm -f "$extension_file" "$tls_dir/server.csr"' EXIT
printf '%s\n' \
  'basicConstraints=CA:FALSE' \
  'keyUsage=digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' \
  "subjectAltName=IP:$lan_ip" > "$extension_file"

openssl x509 -req -sha256 -days 397 -in "$tls_dir/server.csr" \
  -CA "$ca_dir/ca.crt" -CAkey "$ca_dir/ca.key" -CAcreateserial \
  -extfile "$extension_file" -out "$tls_dir/server.crt"

chown 0:10004 "$tls_dir" "$tls_dir/server.key" "$tls_dir/server.crt"
chmod 750 "$tls_dir"
chmod 640 "$tls_dir/server.key"
chmod 644 "$tls_dir/server.crt" "$ca_dir/ca.crt"
chmod 600 "$ca_dir/ca.key"

openssl verify -CAfile "$ca_dir/ca.crt" "$tls_dir/server.crt"
openssl x509 -in "$tls_dir/server.crt" -noout -subject -issuer -dates -ext subjectAltName
echo "Private CA: $ca_dir/ca.crt"
echo "Server TLS files: $tls_dir"
