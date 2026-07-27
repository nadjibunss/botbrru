# Cara Menjalankan botbrru + Cara Ambil Log

Dokumen ini: (A) langkah lengkap menjalankan bot, dan (B) cara mengambil
log agar bisa dikirim balik untuk diperbaiki lebih lanjut.

> **Cara tercepat (1 baris):** setelah unzip, cukup jalankan `bash setup.sh`
> dari dalam folder ini. Skrip akan bikin `.env`, generate `FERNET_KEY`
> otomatis, menanyakan token bot, lalu menjalankan semuanya. Bagian A di
> bawah adalah versi manual/penjelasannya.

---

## A. Menjalankan (cara Docker — disarankan)

### 1. Prasyarat
- Docker + Docker Compose terpasang.
- Sudah punya **bot token** dari [@BotFather](https://t.me/BotFather)
  (kirim `/newbot`, ikuti langkahnya, salin token seperti
  `123456789:AA...`).

### 2. Buat file `.env`
Dari folder proyek:
```bash
cp .env.example .env
```

### 3. Generate FERNET_KEY
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Salin hasilnya ke `.env` pada baris `FERNET_KEY=...`.

> Kalau tidak ada Python di host, jalankan lewat container:
> ```bash
> docker run --rm python:3.11-slim python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```
> (perlu `pip install cryptography` di dalamnya — lebih mudah pakai Python host).

### 4. Isi `.env` minimal
```dotenv
MONGO_URI=mongodb://mongo:27017      # jangan diubah untuk mode docker
MONGO_DB=shopee_monitor
FERNET_KEY=<hasil-generate-di-atas>
MASTER_BOT_TOKEN=<token-dari-BotFather>
LOG_LEVEL=INFO                        # ubah ke DEBUG saat mau diagnosa
```
Sisanya boneh dibiarkan default.

### 5. Jalankan
```bash
docker compose up --build -d
```
- `--build` = build image dari kode.
- `-d` = jalan di background.

### 6. Cek sehat
```bash
curl http://127.0.0.1:8000/health
# harus balas: {"status":"ok","mode":"polling"}
```
Kalau balasan itu muncul, bot sudah jalan dan sedang polling Telegram.

### 7. Setup lewat chat Telegram
Buka chat **private** dengan bot Anda, lalu:
```
/start                → lihat bantuan
/setcredentials       → kirim cookie sesi Shopee (pesan cookie akan dihapus otomatis)
                        lalu kirim risktoken, atau /skip bila tak punya
/setkeywords Gula | Minyak | Beras
/setarea Kab. Bekasi
/setgroup -1001234567890   (opsional: kirim notif ke grup; isi chat_id grup)
/setbot 123456789:AA...    (opsional: pakai bot sendiri untuk notif — silent)
/start_monitor        → mulai memantau
/status               → lihat status & konfigurasi
/stop_monitor         → berhenti
/reset                → kosongkan daftar item yang sudah dicek
```

### 8. Hentikan / restart
```bash
docker compose down          # stop (data Mongo tetap tersimpan di volume)
docker compose restart app   # restart hanya app
docker compose up --build -d # setelah update kode
```

---

## A-alt. Menjalankan tanpa Docker (lokal)

1. Butuh **Python 3.11+** dan **MongoDB** berjalan (mis. `mongodb://localhost:27017`).
2. ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   # isi FERNET_KEY, MASTER_BOT_TOKEN, dan set MONGO_URI=mongodb://localhost:27017
   ```
3. ```bash
   python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
   ```
4. Cek `curl http://127.0.0.1:8000/health`.

---

## A-proxy. Proxy untuk melawan anti-bot (403 / kode 90309999)

Kalau pencarian Shopee balas `403` / `Anti-bot block ... 90309999`, artinya IP
kamu (sering IP VPS) diblokir. Bot ini bisa memakai **daftar proxy** dan
merotasinya otomatis untuk request pencarian.

1. Isi file `proxies.txt` di folder proyek — satu proxy per baris:
   ```
   http://ip:port
   socks5://ip:port
   http://user:pass@ip:port
   ```
   Baris kosong / diawali `#` diabaikan. (File contoh sudah disertakan.)
2. `docker-compose.yml` sudah mem-*mount* `proxies.txt` ke `/app/proxies.txt`
   dan menyetel `PROXY_FILE=/app/proxies.txt`. Cukup edit isinya lalu:
   ```bash
   docker compose restart app
   ```
3. Cek jumlah proxy yang termuat lewat `/status` (baris **Proxy**), atau di log:
   `Loaded N proxies from /app/proxies.txt`.

Cara kerja: tiap pencarian mengambil satu proxy acak. Bila proxy mati atau
kena 403, proxy itu diparkir sementara (cooldown) dan bot mencoba proxy lain
(sampai `PROXY_MAX_TRIES`, default 4). Bila `proxies.txt` kosong/tak ada, bot
jalan langsung tanpa proxy (perilaku lama).

> Realistis: proxy **publik/gratis** umumnya sudah mati atau ikut diblokir
> Shopee, jadi peluang lolosnya kecil. Yang benar-benar bekerja biasanya
> proxy **residential/mobile**. Kalau proxy saja belum cukup, langkah
> berikutnya adalah menyamakan TLS fingerprint (mis. `curl_cffi`
> `impersonate="chrome124"`) — bisa ditambahkan menyusul.

---

## B. Cara ambil log (agar bisa diperbaiki dari log)

### B1. Naikkan detail log saat ada masalah
Di `.env`:
```dotenv
LOG_LEVEL=DEBUG
```
lalu restart:
```bash
docker compose up --build -d
```
DEBUG menampilkan detail internal (klasifikasi respons Shopee, panjang
token, path parsing, dll). Kembalikan ke `INFO` setelah selesai agar log
tidak terlalu ramai.

### B2. Lihat log realtime
```bash
docker compose logs -f app
```
(`Ctrl+C` untuk berhenti melihat; bot tetap jalan.)

### B3. Simpan log ke file untuk dikirim
Ambil, misalnya, 2000 baris terakhir:
```bash
docker compose logs --tail 2000 app > bot-logs.txt
```
Atau rentang waktu tertentu:
```bash
docker compose logs --since 30m app > bot-logs.txt      # 30 menit terakhir
docker compose logs --since 2026-07-26T10:00:00 app > bot-logs.txt
```

Tanpa Docker (kalau dijalankan langsung), arahkan output ke file:
```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 2>&1 | tee bot-logs.txt
```

### B4. WAJIB — sensor rahasia sebelum kirim
Kode ini **tidak** mencetak cookie/token ke log. Tapi tetap:
- **Jangan pernah** kirim isi `.env` (berisi `FERNET_KEY`,
  `MASTER_BOT_TOKEN`, cookie).
- Kalau ragu, sensor cepat:
  ```bash
  sed -E 's/[0-9]{8,}:[A-Za-z0-9_-]{30,}/<TOKEN>/g' bot-logs.txt > bot-logs-aman.txt
  ```
  (mengganti pola token bot bila kebetulan muncul).

### B5. Kirim balik ke sini
Lampirkan `bot-logs.txt` (atau `bot-logs-aman.txt`) ke chat, dan sebutkan:
1. **Apa yang Anda lakukan** (perintah/langkah terakhir).
2. **Apa yang diharapkan** vs **apa yang terjadi**.
3. **Kapan** kira-kira (biar saya cari di rentang waktu yang tepat).

Dari log itu saya bisa telusuri error, temukan penyebabnya, memperbaiki
kode, dan mengujinya lagi.

---

## Penanda log yang berguna (apa artinya)

| Baris log | Arti |
|---|---|
| `Telegram polling started` | Bot mulai menerima update — normal. |
| `MongoDB connected` | Koneksi DB sukses — normal. |
| `Telegram API call failed: getUpdates` + `Telegram polling failure` | Token master salah / Telegram bermasalah. Cek `MASTER_BOT_TOKEN`. Bot otomatis tidur 5 dtk lalu coba lagi. |
| `Restoring worker for telegram_id=...` | Worker dipulihkan setelah restart — normal. |
| `Anti-bot block untuk user=...` | Shopee memblok request (cookie masih hidup). Worker tidur lebih lama lalu lanjut. Butuh risktoken/proxy yang lebih baik. |
| `Sesi Shopee berakhir` (ke user) | Cookie sudah tidak valid — user perlu `/setcredentials` lagi. |
| `__NEXT_DATA__ not found` / `no items in known paths` | Struktur halaman Shopee berubah / hasil kosong. Kirim log DEBUG-nya ke sini. |
| `Worker failed: ...` (+ traceback) | Ada error tak terduga di worker — kirim traceback-nya. |

---

## Masalah umum & solusi cepat

- **`/health` tidak balas** → cek `docker compose ps` (app & mongo up?),
  lalu `docker compose logs app`.
- **App exit / crash saat start** → biasanya `.env` kurang `FERNET_KEY`
  atau `MASTER_BOT_TOKEN`, atau Mongo belum siap. Cek log.
- **Bot tidak balas perintah** → pastikan chat **private**, token benar,
  dan `Telegram polling started` muncul di log.
- **Kena anti-bot terus** → butuh risktoken valid dari browser dan/atau
  `PROXY_URL` residential (lihat `.env.example`).
