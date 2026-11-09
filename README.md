# SubZero

Soğuk Savaş döneminde geçen, terminalde oynanan akustik bir denizaltı roguelike'ı. Yaşlı bir dizel-elektrik
istihbarat denizaltısının sonar operatörüsünüz. Hiçbir şey göremezsiniz, yalnızca dinlersiniz.

```
./subzero.sh                    # başlatıcı betik (argümanları oyuna iletir, ör. ./subzero.sh --sound)
python3 -m subzero              # oyna (en az 100x30 terminal, 150x45 önerilir)
python3 -m subzero --seed 42    # aynı okyanus, aynı koşu
python3 -m subzero --sound      # paplay / pw-play / aplay ile sentezlenmiş PCM sesler
python3 -m subzero --no-bell    # terminal zilini kapat
```

Bağımlılık yok, yalnızca standart kütüphane (`curses`). İsterseniz `pip install -e .` ile kurup `subzero` komutuyla da çalıştırabilirsiniz.

## Ekran

| Panel | İçerik |
|---|---|
| **PPI** (sol üst) | Kuzey yukarıda. Dönen tarama çizgisi, kuyruklu ve sönerek kaybolan temas izleri, haritadaki batimetri tehlikeleri (`░` yakın, `▒` bulunduğunuz derinlikte çarpışma), kör sektör (baffles, kırmızı kenar), hedef `X` |
| **BTR / LOFAR** (sol alt) | Kerteriz-zaman şelalesi veya seçili temasın 0–600 Hz frekans şelalesi |
| **Telemetri** (sağ üst) | Rota/hız/derinlik, salma altı, termoklin, gövde, basınç, batarya, O2/CO2, gürültü imzası, kovanlar, uyarılar, temas listesi, dikey derinlik profili |
| **CONN** (sağ alt) | Olay günlüğü ve komut satırı |

Semboller: `@` siz · `?` bilinmeyen · `!` düşman · `#` tarafsız · `~` biyolojik · `*` torpido · `+` bizim torpido · `o` dekoy · `X` çıkış noktası

## Görev

Belirtilen sayıda düşman askerî unsurun (savaş gemisi / denizaltı) akustik imzasını kaydedin, sonra `X` çıkış noktasına ulaşın.
İmza kaydetmek için teması seçin (`TRACK S3` veya Tab) ve LOFAR'da yeterince güçlü sinyalle tutun. Başarılı devriyeden sonra
bir sonraki, daha zor devriyeye geçersiniz. Ölüm kalıcıdır.

## Komutlar

```
HELM HDG 140 SPD 5 DEPTH 300     (HELM öneki isteğe bağlı)
LEFT 30 · RIGHT 90 · ALL STOP · FLANK · PD
SONAR PASSIVE BROAD · SONAR PASSIVE NARROW 045 · SONAR PING  (veya PING)
TRACK S3 · BTR · CLASSIFY S3 MERCHANT|TRAWLER|BIO|WARSHIP|SUB|UNKNOWN
TUBE 1 LOAD TORP|DECOY · TUBE 1 UNLOAD · TUBE 1 FIRE [S3|kerteriz]
SILENT RUN ON|OFF · SCRUB ON|OFF · SNORKEL ON|OFF
RANGE 2..32 · TIME 1..32 · LIB · MISSION · HELP · QUIT
```

Tuşlar: `Enter` çalıştır · `↑↓` geçmiş · `Tab` sonraki temas · `PgUp/PgDn` günlük · `[ ]` zaman sıkıştırma ·
`< >` skop menzili · `F2` ping · `F3` BTR/LOFAR

## Taktik notları

- **Termoklin**: katmanın karşı tarafındaki sesler 10–17 dB zayıflar. Savaş gemilerinin gövde sonarı katmanın üstünde kalır;
  altına inmek aktif ping'lerini de büyük ölçüde etkisiz kılar. Bazı gemilerde değişken derinlik sonarı (VDS) vardır.
- **Ping** tüm temasları kesin konumlarıyla gösterir, ama okyanustaki herkes sizin yerinizi öğrenir.
- **Pasif** izlerde menzil tahminidir (`~`); rotanızı 30°'den fazla değiştirmek (yeni ayak) çözümü iyileştirir.
- **Gürültü**: hız, kavitasyon (sığda ve hızlıyken), scrubber'lar ve şnorkel sizi duyulur kılar. Sessiz seyir 6 dB kazandırır ama
  scrubber'ları kapatır; CO2'yi izleyin.
- **Batarya** şnorkel ile periskop derinliğinde (18 m, ≤8 kt) şarj olur; bu en savunmasız anınızdır.
- **Kanyon / dip maskelemesi**: kanyon duvarlarının altına inmek veya dibe yakın seyretmek tespit edilmenizi zorlaştırır.
- **Torpido gelirse**: dekoy atın, sert dönün, katmanı geçin. Düşman torpidolarının yakıtı sınırlıdır.
- Tarafsız gemi batırmak ağır puan cezası getirir ve bölgedeki tüm savaş gemilerini üzerinize çeker. `LIB` ile imza kütüphanesine bakın.

Skorlar `~/.subzero_scores.json` dosyasında tutulur.
