# Kahramankart Ulaşım API

Kahramanmaraş otobüs hatları, durakları, canlı araç konumları ve sefer saatleri için sade bir JSON API'si. Python/FastAPI ile yazıldı; [Maraş Ulaşım](https://github.com/uzaylikafasi/maras-ulasim) Android uygulaması bunu kullanır.

**Hazır sunucu:** `https://mithrixa.com/ulasim-api` · **[Doküman](https://mithrixa.com/ulasim-api/docs)** · [openapi.json](https://mithrixa.com/ulasim-api/openapi.json)

> Resmî bir hizmet değildir. Belediye veya Kahramankart ile bağlantısı yoktur. Veriler kaynağın verdiği gibidir; varış süreleri tahmindir.

## Hızlı deneme

```sh
curl 'https://mithrixa.com/ulasim-api/v1/search?q=H/1'
curl 'https://mithrixa.com/ulasim-api/v1/lines/9576/vehicles?direction=1'
```

Hazır sunucuda anahtar gerekmez. Bir IP dakikada en fazla 90 istek yapabilir; canlı araç verisini 15 saniyeden sık sorgulamayın.

## Uçlar

| Yol | Ne verir |
|---|---|
| `/v1/search?q=` | Hat, durak ve adres araması |
| `/v1/stops/nearby?latitude=&longitude=` | Yakındaki duraklar |
| `/v1/stops/in-bounds?south=&west=&north=&east=` | Harita alanındaki duraklar |
| `/v1/stops/{id}` | Durak ve yaklaşan otobüsler |
| `/v1/stops/{id}/lines` | Duraktan geçen hatlar |
| `/v1/lines/{id}/vehicles?direction=` | Hattaki araçların canlı konumu |
| `/v1/lines/{id}/stops?direction=` | Hattın durakları |
| `/v1/lines/{id}/shape?direction=&format=geojson` | Güzergâh çizgisi |
| `/v1/timetables?line_code=` | Sefer saatleri |
| `/v1/announcements` | Duyurular |
| `/v1/journeys?from_stop_id=&to_stop_id=` | İki durak arası yolculuk önerisi |
| `/healthz` | Sunucu ayakta mı |

Tam liste ve alanlar için [dokümana](https://mithrixa.com/ulasim-api/docs) bakın. Boş araç listesi "şu an veri yok" demektir; konum uydurulmaz.

## Kendi sunucunda çalıştırma

Python 3.11+ gerekir.

```sh
cp .env.example .env    # ayarları düzenle
./run.sh                # .venv yoksa kurar, http://127.0.0.1:8000 adresinde başlatır
```

Kaynak servise erişim bilgisi depoda yoktur. `.env` içinde `PALLIUM_AUTH_FILE` (yerel JSON dosyası) ya da `PALLIUM_AUTHORIZATION` (tam başlık değeri) ile verilir; istemci koduna veya tarayıcıya gönderilmez.

İsteğe bağlı ayarlar:

- `API_HOST`, `API_PORT`: dinlenecek adres (başka cihazlardan erişim için `API_HOST=0.0.0.0`)
- `API_ACCESS_KEY`: ayarlanırsa `/v1` çağrıları `X-API-Key` başlığı ister
- `CORS_ORIGINS`: tarayıcıdan erişebilecek siteler

Yerel sunucuda etkileşimli doküman `http://127.0.0.1:8000/docs` adresindedir.

## Dosyalar

- `transport_api/`: sunucu (FastAPI) ve kaynak servis adaptörü
- `deployment/`: herkese açık köprü (`transport_proxy.py`: yalnız GET, IP başına sınır), doküman sayfası, systemd örneği
- `examples/client.mjs`: JavaScript istemci örneği
- `tools/`: durak listesi üretme betikleri
- `tests/`: `pytest` ve `node --test tests/client.test.mjs`

## Lisans

MIT
