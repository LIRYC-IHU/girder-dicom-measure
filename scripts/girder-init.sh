#!/bin/sh
# Provisioning d'un Girder neuf pour le dev/test : compte admin + assetstore.
# Idempotent (les créations échouent silencieusement si déjà faites).
set -u

GIRDER="http://girder:8080/api/v1"

echo "[init] attente de Girder..."
i=0
until curl -sf "$GIRDER/system/version" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo "[init] Girder injoignable, abandon"; exit 1; fi
  sleep 3
done
echo "[init] Girder est prêt."

# 1) Compte admin (le tout premier utilisateur Girder devient admin).
echo "[init] création du compte admin..."
curl -s -X POST "$GIRDER/user" \
  --data-urlencode "login=admin" \
  --data-urlencode "password=password" \
  --data-urlencode "email=admin@example.com" \
  --data-urlencode "firstName=Admin" \
  --data-urlencode "lastName=User" >/dev/null 2>&1 || true

# 2) Token via Basic Auth.
TOKEN=$(curl -s -u admin:password "$GIRDER/user/authentication" \
  | sed -E 's/.*"token" *: *"([^"]+)".*/\1/')

if [ -z "$TOKEN" ] || [ "${#TOKEN}" -lt 10 ]; then
  echo "[init] impossible d'obtenir un token (compte déjà existant ?) — on s'arrête là."
  exit 0
fi

# 3) Assetstore filesystem (type 0) si aucun n'existe.
if [ "$(curl -s -H "Girder-Token: $TOKEN" "$GIRDER/assetstore" | tr -d ' \n')" = "[]" ]; then
  echo "[init] création de l'assetstore filesystem..."
  curl -s -H "Girder-Token: $TOKEN" -X POST "$GIRDER/assetstore" \
    --data-urlencode "name=local" \
    --data-urlencode "type=0" \
    --data-urlencode "root=/assetstore" >/dev/null 2>&1 || true
fi

echo "[init] terminé. Connexion : admin / password"
