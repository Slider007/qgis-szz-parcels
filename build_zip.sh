#!/bin/sh
# Собирает dist/szz_parcels-<версия>.zip для «Модули → Установить из ZIP».
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version=//p' szz_parcels/metadata.txt)
mkdir -p dist
ZIP="dist/szz_parcels-$VERSION.zip"
rm -f "$ZIP"
zip -qr "$ZIP" szz_parcels -x '*__pycache__*' '*.pyc' '*.DS_Store'
echo "$ZIP"
