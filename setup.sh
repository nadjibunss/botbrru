#!/usr/bin/env bash
# Setup 1x jalan: bikin .env, generate FERNET_KEY, minta token bot, lalu run.
# Pakai: bash setup.sh
set -e
cd "$(dirname "$0")"

# Ganti satu baris di .env dengan cara portable (GNU & BSD/macOS sed).
replace_line() {
  local pattern="$1" value="$2" file=".env" tmp
  tmp="$(mktemp)"
  sed "s|^${pattern}=.*|${pattern}=${value}|" "$file" > "$tmp" && mv "$tmp" "$file"
}

echo "== Setup botbrru =="

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker belum terpasang. Pasang Docker + Docker Compose dulu."
  exit 1
fi

# 1. .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[1/4] .env dibuat dari template"
else
  echo "[1/4] .env sudah ada -> dipakai ulang"
fi

# 2. Build image Docker
echo "[2/4] Build image Docker (butuh beberapa menit pertama kali)..."
docker compose build

# 3. FERNET_KEY (generate otomatis jika masih placeholder)
if grep -q "GANTI_DENGAN_FERNET_KEY_VALID" .env; then
  FERNET="$(docker compose run --rm --no-deps -T app \
    python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  replace_line "FERNET_KEY" "$FERNET"
  echo "[3/4] FERNET_KEY digenerate otomatis"
else
  echo "[3/4] FERNET_KEY sudah terisi -> dilewati"
fi

# 4. Token bot (minta sekali; input aman karena dari file skrip, bukan paste baris)
if grep -q "GANTI_DENGAN_TOKEN_BOT" .env; then
  echo
  echo "Ambil token dari @BotFather (kirim /newbot). Contoh: 123456789:AAH...xyz"
  read -r -p "[4/4] Tempel TOKEN bot lalu tekan Enter: " TOKEN
  if [ -z "$TOKEN" ]; then
    echo "ERROR: token kosong. Jalankan lagi: bash setup.sh"
    exit 1
  fi
  replace_line "MASTER_BOT_TOKEN" "$TOKEN"
else
  echo "[4/4] MASTER_BOT_TOKEN sudah terisi -> dilewati"
fi

echo
echo "== Menjalankan bot =="
docker compose up -d

echo "== Menunggu & cek /health =="
sleep 6
if curl -s http://127.0.0.1:8000/health | grep -q '"status":"ok"'; then
  echo
  echo "BERHASIL. Bot jalan. Buka chat PRIVATE dengan bot Anda, ketik /start."
else
  echo
  echo "Belum merespons. Lihat log: docker compose logs --tail 50 app"
fi
