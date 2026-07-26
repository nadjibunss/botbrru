# Changelog — perbaikan botbrru

Semua perubahan di bawah sudah diverifikasi dengan menjalankan kode
(kompilasi, analisis statis import/atribut, unit test logika, dan uji
handler end-to-end memakai MongoDB & Telegram tiruan). Total 118
pemeriksaan otomatis lolos.

## Bug yang diperbaiki (crash / salah)

1. **`config.py` — 13 field settings hilang.**
   `utils/headers.py` dan `services/shopee_client.py` mengakses
   `settings.user_agent`, `settings.sec_ch_ua*`, `settings.proxy_url`,
   `settings.request_timeout`, `settings.max_retries`,
   `settings.retry_backoff_base`, `settings.shopee_*` yang tidak pernah
   didefinisikan → `AttributeError` saat import (headers.py membangun dict
   di level modul). Ditambahkan semua field dengan default masuk akal dan
   bisa di-override lewat `.env`.

2. **`services/shopee_client.py` — import salah folder.**
   `from src.services.fingerprint import ...` padahal file ada di
   `src/utils/fingerprint.py` → `ModuleNotFoundError`. Diperbaiki ke
   `src.utils.fingerprint`.

3. **`db.py` — shim meng-import nama yang tidak ada.**
   `from src.utils.database import create_indexes, get_client, get_db` —
   dua nama pertama tidak ada di `database.py` → `ImportError`. Diganti ke
   nama yang benar-benar ada (`close_db, get_db, is_db_available`).

4. **`services/monitor_worker.py` — ekstraksi item salah (jalur inti bot).**
   Loop notifikasi membaca `itemid`/`shopid` hanya dari level atas item,
   padahal Shopee menaruh field inti di dalam `item_basic`. Akibatnya link
   rusak (`...-i.None.None`) dan hanya item pertama yang ter-notif (sisanya
   tersuppress karena `None == None` di dedup, lalu `None` mencemari
   `checked_items`). Ditambahkan helper `_extract_item_fields()` (baca level
   atas → fallback `item_basic`) + guard melewati entri tanpa id valid.

5. **`utils/telegram.py` — polling hot-loop saat API error.**
   Bila `getUpdates` gagal (token salah / Telegram down), `_get`
   mengembalikan `{"ok": False}`, `get_updates` mengembalikan `[]`, dan
   `polling_loop` langsung loop lagi tanpa jeda (ribuan panggilan/detik).
   `get_updates` sekarang melempar `TelegramAPIError` saat gagal sehingga
   `polling_loop` masuk cabang `except` dan tidur 5 detik.

## Perbaikan ketahanan / konsistensi

6. **`handlers/commands.py` — shallow copy default user.**
   `DEFAULT_USER_DOC.copy()` membuat semua user baru berbagi objek list/dict
   `checked_items` & `setup_payload` yang sama. Diganti `copy.deepcopy`.

7. **`services/monitor_worker.py` — `checked_items` tumbuh tanpa batas.**
   Dibatasi ke `MAX_CHECKED_ITEMS = 2000` id terakhir (mencegah dokumen
   Mongo membengkak menuju limit 16MB) + memakai `set` untuk cek membership
   O(1).

8. **`handlers/commands.py` — risktoken tidak dipakai saat pre-check.**
   `command_start_monitor` memvalidasi cookie tanpa risktoken, padahal
   worker memakainya (bisa memicu "sesi tidak valid" palsu). Risktoken
   tersimpan kini ikut diteruskan ke `validate_cookie`.

9. **`handlers/commands.py` — batas panjang ID token bot terlalu ketat.**
   `8–11` digit → `8–16`, mengakomodasi ID Telegram baru yang lebih panjang.

10. **`utils/fingerprint.py`** — docstring path salah (`src/services/...` →
    `src/utils/...`). **`.env.example`** ditambah knob opsional
    (`PROXY_URL`, `REQUEST_TIMEOUT`, `MAX_RETRIES`, dst).

## Catatan (sengaja tidak diubah — perlu Shopee sungguhan untuk uji)

- Ada dua klien Shopee paralel: `monitor_worker` (inline, dipakai app) dan
  `shopee_client.py` (scraping `__NEXT_DATA__`, lebih tahan anti-bot, belum
  terpakai). Keduanya kini bisa jalan; beralih ke yang kedua adalah
  perubahan perilaku yang perlu diuji ke Shopee asli.
- `session_service.validate_cookie` menganggap HTTP 200 + kode anti-bot
  (non-captcha) sebagai "valid tanpa username". Dibiarkan agar tidak
  memblokir pengguna yang cookie-nya sebenarnya hidup; worker sudah
  menangani anti-bot dengan backoff.

## Cara jalan

1. Salin `.env.example` → `.env`, isi `FERNET_KEY` dan `MASTER_BOT_TOKEN`.
   - `FERNET_KEY`: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `MASTER_BOT_TOKEN`: dari @BotFather.
2. `docker compose up --build`
3. Cek `http://127.0.0.1:8000/health` → `{"status":"ok","mode":"polling"}`.
