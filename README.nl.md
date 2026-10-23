# phasex64-linux

Linux-driver en configuratiescherm voor de PHASE X64 USB-audio-interface.

![Licentie: GPL-2.0-only](https://img.shields.io/badge/licence-GPL--2.0--only-blue)
![Status: publieke bèta](https://img.shields.io/badge/status-public%20beta-orange)
[![Release](https://img.shields.io/github/v/release/flarkflarkflark/phasex64-linux?include_prereleases&sort=semver)](https://github.com/flarkflarkflark/phasex64-linux/releases)
[![build](https://github.com/flarkflarkflark/phasex64-linux/actions/workflows/build.yml/badge.svg)](https://github.com/flarkflarkflark/phasex64-linux/actions/workflows/build.yml)

> Vertaling van [README.md](README.md), dat leidend is. Bij verschil geldt
> de Engelse tekst.

Releases: [alle releases](https://github.com/flarkflarkflark/phasex64-linux/releases) ·
Wijzigingen: [CHANGELOG.md](CHANGELOG.md) (Engels)

## Wat het is

De PHASE X64 USB heeft geen class-compliant USB-audiomodus, dus de
standaarddriver `snd-usb-audio` van Linux kan er niets mee. Dit project
levert:

- **`snd-usb-phasex64`** - een ALSA-kerneldriver, gebouwd en bij
  kernelupdates bijgehouden met DKMS.
- **`phasex64-panel`** - een configuratiescherm (Python/Tk) voor
  routering, opnamebronnen, phono-RIAA, meters, MIDI-activiteit en de
  buffergrootte van PipeWire. Het is niet afhankelijk van een bepaalde
  desktopomgeving.
- **`phasex64-wireplumber`** (optioneel) - een WirePlumber-profiel dat de
  kaart op het "Pro Audio"-profiel van PipeWire houdt, met native formaat
  en samplerate, voor gebruik met JACK-applicaties zoals REAPER.

Ook bekend als / zoektermen: TerraTec PHASE X64 USB, TerraSoniq PHASE
X64, Musonik PHASE X64, Ploytec-chipset, TerraTec Phase X64 Linux-driver.
Deze namen worden alleen gebruikt om aan te geven met welke hardware dit
werkt.

## Status

**v0.9.1, publieke bèta.** Getest met één exemplaar, op twee machines
(zie [Geteste omgeving](#geteste-omgeving)).

Wat werkt:

- Afspelen, 4 kanalen (Out 1/2 en Out 3/4), 24 bit.
- Opnemen, 6 kanalen: In 1/2, In 3/4, en kanalen 5/6, een bit-exacte
  digitale kopie van wat naar Out 1/2 gaat (om op te nemen wat je hoort).
- Phono-ingangen met de RIAA-correctie in de driver, zoals de
  Windows-driver dat doet.
- Codec- en routeringswijzigingen terwijl een stream loopt (live
  toegepast, met een gat van ~0,3 s waarbij de tijdlijn van de stream
  doorloopt).
- MIDI in en uit (5-polige DIN).
- PipeWire, en JACK-applicaties via `pipewire-jack`; bit-exact door
  PipeWire als het sink- en sourcevolume van de kaart op 100 % staat.
- Buffergroottes tot 64 samples (1,3 ms bij 48 kHz).
- Samplerates (afspelen en opnemen delen altijd één samplerate):
  - **48 kHz** - dagelijks gebruik, en de samplerate van de functietests
    sinds driver v0.1.10 (live wijzigingen, MIDI, PipeWire/REAPER,
    buffergroottes).
  - **44,1, 88,2 en 96 kHz** - ondersteund, en één keer op hardware
    getest, met driver v0.1.10 op 2026-10-14 (direct via ALSA, zonder
    PipeWire): per samplerate 12 s tegelijk afspelen en opnemen, een
    toon van 1 kHz via een analoge loopback - realtime, de door het
    apparaat teruggemelde samplerate klopte, geen fouten - plus wisselen
    48 → 44,1 → 96 → 48 kHz zonder opnieuw in te pluggen. 44,1 kHz is
    ook gebruikt voor de bit-exactheidstest van opnamekanalen 5/6
    (2026-10-16). Sindsdien niet opnieuw getest, en niet via PipeWire of
    REAPER.
- Klokbron: intern, of vergrendeld op de S/PDIF-ingang.

### Geteste omgeving

Op hardware (één exemplaar):

- **Machine 1** (alles hierboven): Arch Linux, kernel 7.2.x, PipeWire
  1.6.x, WirePlumber 0.5.x, REAPER 7.82 via `pipewire-jack`.
- **Machine 2:** EndeavourOS, kernel 6.18 LTS (DKMS bouwde de module ook
  voor 7.2), een andere AMD-xHCI-USB-controller, het apparaat aangesloten
  via een USB-hub naast andere USB-audioapparaten. Tegelijk afspelen en
  opnemen draait stabiel; het WirePlumber-profiel heeft alleen invloed op
  de PHASE X64, niet op de andere audioapparaten.

Buildtest in CI bij elke push, tegen de eigen kernelheaders van elke
distributie (zonder hardware;
[buildstatus](https://github.com/flarkflarkflark/phasex64-linux/actions/workflows/build.yml)):

| Distributie | Kernel |
|---|---|
| Ubuntu 22.04 | 5.15 |
| Ubuntu 24.04 | 6.8 |
| Debian 13 (stable) | 6.12 |
| Arch Linux | 7.2 |
| Fedora 44 | 7.2 |

(Kernelversies op 2026-10-22; CI gebruikt altijd de actuele headers van
de distributies.)

## Installatie

Kies een van de vier manieren hieronder. In alle gevallen bouwt DKMS de
driver op je eigen machine, voor jouw kernel - er wordt geen kant-en-klare
kernelmodule meegeleverd - dus de kernelheaders van je kernel moeten
geïnstalleerd zijn.

<a id="snel-aan-de-slag-arch-linux--manjaro"></a>

### 1. Arch Linux, Manjaro, EndeavourOS: releasepakketten

Download de driver- en configuratieschermpakketten van de
[nieuwste release](https://github.com/flarkflarkflark/phasex64-linux/releases),
controleer ze met `SHA256SUMS` en installeer ze:

```sh
sudo pacman -S --needed linux-headers   # de headers van jouw kernel, zie hieronder
v=0.9.1
base=https://github.com/flarkflarkflark/phasex64-linux/releases/download/v$v
curl -L --remote-name-all "$base/SHA256SUMS" \
  "$base/snd-usb-phasex64-dkms-$v-1-any.pkg.tar.zst" \
  "$base/phasex64-panel-$v-1-any.pkg.tar.zst"
sha256sum -c --ignore-missing SHA256SUMS   # elk bestand moet "OK" geven
sudo pacman -U "snd-usb-phasex64-dkms-$v-1-any.pkg.tar.zst" \
  "phasex64-panel-$v-1-any.pkg.tar.zst"
```

- Gebruik het headerspakket van je kernel: `linux-lts-headers`,
  `linux-zen-headers`, `linuxXYZ-headers` op Manjaro, ... `dkms` komt mee
  als afhankelijkheid.
- Eerst downloaden, dan de lokale bestanden installeren: de pakketten zijn
  niet met GPG ondertekend, en met de standaardinstellingen van Arch
  weigert `pacman -U` niet-ondertekende pakketten als URL (als lokaal
  bestand accepteert hij ze wel). `SHA256SUMS` is in plaats daarvan de
  controle.

Daarna de kaart uit- en weer inpluggen, of de module direct laden, en het
configuratiescherm starten via het applicatiemenu ("PHASE X64 USB") of
met `phasex64-panel`:

```sh
sudo modprobe snd-usb-phasex64
cat /sys/module/snd_usb_phasex64/version   # 0.9.1
```

Optioneel, voor JACK/REAPER: het WirePlumber-profiel. Lees eerst
[PipeWire / JACK](linux/README.nl.md#pipewire--jack) - het geldt voor
alle gebruikers op de machine (het verandert alleen hoe de PHASE X64
wordt behandeld). In dezelfde shell als hierboven:

```sh
curl -L -O "$base/phasex64-wireplumber-$v-1-any.pkg.tar.zst"
sha256sum -c --ignore-missing SHA256SUMS
sudo pacman -U "phasex64-wireplumber-$v-1-any.pkg.tar.zst"
systemctl --user restart wireplumber
```

Bijwerken: hetzelfde met de nieuwere versie; verwijderen:
`sudo pacman -R snd-usb-phasex64-dkms phasex64-panel phasex64-wireplumber`.

### 2. AUR

`snd-usb-phasex64-dkms`, `phasex64-panel` en `phasex64-wireplumber`
(pakketbasis `phasex64`) - binnenkort.

<a id="andere-distributies"></a>

### 3. Elke andere distributie: release-tarball + DKMS

```sh
v=0.9.1
base=https://github.com/flarkflarkflark/phasex64-linux/releases/download/v$v
curl -L --remote-name-all "$base/SHA256SUMS" "$base/phasex64-linux-$v.tar.gz"
sha256sum -c --ignore-missing SHA256SUMS   # moet "OK" geven
tar xzf "phasex64-linux-$v.tar.gz"
cd "phasex64-linux-$v"
scripts/install-dkms.sh --dry-run   # vereisten controleren, stappen tonen
sudo scripts/install-dkms.sh
sudo modprobe snd-usb-phasex64      # of de kaart uit- en weer inpluggen
```

Het script controleert of dkms, make, gcc en de headers van je draaiende
kernel aanwezig zijn, en noemt de pakketten voor apt, dnf, zypper of
pacman als er iets ontbreekt. DKMS bouwt de module na elke kernelupdate
opnieuw. Verwijderen met `sudo scripts/uninstall-dkms.sh`. Details:
[Installeren op andere distributies](linux/README.nl.md#installeren-op-andere-distributies).

**Secure Boot:** met Secure Boot aan laden alleen ondertekende modules;
een niet-ondertekende faalt met `Key was rejected by service`. DKMS
ondertekent de module, en de sleutel moet één keer worden ingeschreven
(MOK) bij een herstart - zie
[Secure Boot](linux/README.nl.md#secure-boot). Controleren met
`mokutil --sb-state`.

**Configuratiescherm:** installeer de afhankelijkheden en start
`linux/phasex64-panel/phasex64-panel` vanuit de uitgepakte map
([menu-item en details](linux/README.nl.md#configuratiescherm-zonder-pakket)).

| Distributie | Vereist | Optioneel: BUFFER-regeling |
|---|---|---|
| Debian, Ubuntu | `python3 python3-tk alsa-utils` | `pipewire-bin` |
| Fedora | `python3 python3-tkinter alsa-utils` | `pipewire-utils` |
| openSUSE | `python3 python3-tk alsa-utils` | `pipewire-tools` |
| Arch | `python tk alsa-utils` | `pipewire` |

`setpriv` uit util-linux (bijna overal al geïnstalleerd) wordt ook
gebruikt als het er is.

### 4. Vanuit de broncode (ontwikkelaars)

```sh
git clone https://github.com/flarkflarkflark/phasex64-linux.git
cd phasex64-linux
```

Bouw daarna op Arch-gebaseerde distributies de pakketten zelf
(eerst `sudo pacman -S --needed base-devel dkms linux-headers`):

```sh
cd linux
makepkg -s
sudo pacman -U snd-usb-phasex64-dkms-*.pkg.tar.zst phasex64-panel-*.pkg.tar.zst
```

of, overal, `sudo scripts/install-dkms.sh` vanuit de checkout, of bouw de
module met de hand met `make` - zie
[Installeren op Arch Linux](linux/README.nl.md#installeren-op-arch-linux).
De volledige handleiding staat in [linux/README.nl.md](linux/README.nl.md).

## Schermafbeeldingen

Alle schermafbeeldingen komen uit de demomodus van het configuratiescherm
(geen apparaat aangesloten, geen artwork van de fabrikant). De
interface is hier in het Engels.

![Hoofdvenster met de preset "Record vinyl": In 1/2 op Phono met RIAA, meters lopen](docs/screenshots/main-window.png)

*Hoofdvenster, preset "Record vinyl" geladen.*

![Beide ingangen op een Loopback-bron met Direct op beide uitgangen; het configuratiescherm waarschuwt voor de analoge terugkoppeling](docs/screenshots/loopback-feedback-warning.png)

*Loopback-bronnen met de waarschuwing voor analoge terugkoppeling.*

![MIDI IN-monitor met note-, controller-, pitch-bend-, program-change- en SysEx-berichten](docs/screenshots/midi-monitor.png)

*MIDI IN-monitor (voorbeeldberichten).*

![BUFFER-regeling op 128 samples, met 2,7 ms](docs/screenshots/buffer-control.png)

*Bufferregeling: de buffergrootte van PipeWire, met de latentie bij de
huidige samplerate.*

## Hardware

Een USB 2.0-interface, rond 2010-2011 uitgebracht onder de naam
TerraSoniq door Musonik, dat de Producer-lijn van TerraTec (met de
PHASE-serie) distribueerde en onder een eigen merk voortzette; de eigen
tekst van de fabrikant presenteerde hem als opvolger van de PHASE 26 USB
([KVR Audio-forumdraad, 2011](https://www.kvraudio.com/forum/viewtopic.php?t=309484)).
Hij wordt niet meer verkocht
([productpagina Musikhaus Korn](https://www.musikhaus-korn.de/en/terrasoniq-phase-x64-usb/pd/36185)).
Genoemde eigenschappen: 24 bit / 96 kHz, een mic/line-combo-ingang met
48 V fantoomvoeding, een Hi-Z-instrumentingang, twee inserts,
phono/aux-ingangen, coaxiale S/PDIF in en uit, een koptelefoonuitgang,
MIDI in en uit, en voeding via USB of een aparte adapter (zelfde
productpagina;
[productpagina en gebruikersreview op Audiofanzine](https://en.audiofanzine.com/usb-interface/terratec/terrasoniq-phase-x64-usb/)).

Binnenin, gezien vanuit de driver: een Ploytec-USB-controller (USB-ID
`0a4a:5b01`) en twee Wolfson WM8776-codecs.

Aansluitingen op het exemplaar dat voor dit project is gebruikt:

- **Voorkant:** phono/aux-RCA-ingangen voor Ch.1/2 en Ch.3/4; Ch.1
  mic-combo-aansluiting (XLR + 6,3 mm jack) met Mic/Line-gainknop, +48 V-
  en clip-led; Ch.2 Hi-Z/Line-jack met gainknop en clip-led;
  koptelefoonaansluiting met volumeknop; aardklem (⏚) tussen de
  RCA-ingangen en de combo-aansluiting van Ch.1; "Run"-led naast de
  Hi-Z-gainknop.
- **Achterkant:** voeding (9 V DC), USB-B, MIDI out/in, S/PDIF (coaxiaal)
  out/in, Out 3/4 (RCA), inserts voor Ch.1 (Mic/Line) en Ch.2
  (Hi-Z/Line), Out 1/2 (6,3 mm jacks).
- **Schakelaars aan de zijkant:** 48 V aan/uit, Ch.1 Line/Mic, Ch.2
  Line/Hi-Z, koptelefoonbron 1/2 of 3/4, Ch.1/2 Aux/Phono, Ch.3/4
  Aux/Phono. Die zijn puur hardware; de driver kan ze niet lezen of
  instellen.

![Voorpaneel van de PHASE X64 USB: gainknoppen met de leds +48V, Run en Clip, de Phono/Aux-RCA-ingangen, de aardklem daaronder, de combo-aansluiting van Ch.1 en de Phones- en Hi-Z/Line-jacks](docs/hardware/phasex64-front.jpg)

*Voorkant. De aardklem (⏚) onderaan, tussen de RCA-ingangen en de
combo-aansluiting van Ch.1, is voor de aardedraad van de platenspeler; de
rode "Run"-led betekent verbonden via USB / stroom aan.*

![Achterpaneel: voeding en USB, MIDI-uitgang en -ingang, S/PDIF uit en in, Out 3/4 (RCA), de twee inserts en Out 1/2](docs/hardware/phasex64-back.jpg)

*Achterkant. Voeding, USB, MIDI, S/PDIF, Out 3/4, inserts, Out 1/2.*

![Zijkant van het apparaat met zes schuifschakelaars: 48V, Ch.1 Line/Mic, Ch.2 Line/HiZ, Phones Ch. 1/2 of 3/4, Ch.1/2 Aux/Phono, Ch.3/4 Aux/Phono](docs/hardware/phasex64-side.jpg)

*Zijkant. De schakelaars die puur hardware zijn: 48 V, Ch.1 Line/Mic,
Ch.2 Line/Hi-Z, koptelefoonbron, Ch.1/2 en Ch.3/4 Aux/Phono.*

Foto's: het eigen exemplaar van de auteur, CC BY 4.0 (zie
[docs/hardware/](docs/hardware/README.md)).

**Platenspeler:** sluit de aardedraad van de platenspeler aan op de
aardklem (⏚) aan de voorkant, tussen de RCA-ingangen en de
combo-aansluiting van Ch.1, om brom te voorkomen.

**"Run"-led:** betekent dat het apparaat via USB verbonden is en stroom
heeft. Op hardware gecontroleerd: hij blijft branden met de driver
geladen terwijl er geen PCM-stream loopt (beide PCM's gesloten,
PipeWire-nodes gesuspend), dus hij laat niet zien of er audio loopt.

### Gemeten met deze driver

- **Frontingangen:** Ch.1 komt binnen op het linkerkanaal van In 1/2,
  Ch.2 op het rechter, beide mono. De schakelaar van Ch.1 kiest de
  aansluiting: Mic gebruikt het XLR-deel, Line het jack-deel. 48 V staat
  alleen op de XLR-pin. De gain is analoog en stel je in met de knoppen op
  het apparaat; de clip-led gaat aan bij 0 dBFS op de converter. Mic-gain
  beslaat ongeveer 25 dB, Line ongeveer 14 dB, Hi-Z minstens 30 dB.
  Details: [Frontingangen](linux/README.nl.md#frontingangen-mic--hi-z--line).
- **Direct monitoring:** Direct houdt Ch.1 links en Ch.2 rechts; Direct
  Mono telt beide bij elkaar op naar beide kanten.
- **Twee soorten loopback:** opnamekanalen 5/6 zijn een digitale kopie
  van de PCM die naar Out 1/2 gaat, voordat Direct monitoring wordt
  bijgemengd. De opnamebronnen `Loopback 3/4` / `Loopback 1/2` nemen de
  analoge uitgang van het *andere* uitgangspaar op, inclusief Direct.
  Details: [Indeling opnamekanalen](linux/README.nl.md#indeling-opnamekanalen).
- **Buffergroottes:** 64, 128, 256 en 1024 samples draaien stabiel met
  tegelijk afspelen en opnemen; een live wijziging kan één korte klik
  geven.
- **Latentie:** bij 64 samples kwam een analoge loopback-opname ongeveer
  54 samples (≈1,1 ms) later binnen dan REAPER's eigen compensatie
  verrekent. Dat is een eerste, benaderende meting. Details:
  [Buffergrootte en latentie](linux/README.nl.md#buffergrootte-en-latentie).
- **MIDI heeft een audiostream nodig:** de firmware van het apparaat
  geeft MIDI alleen door terwijl er een audiostream loopt (alleen een
  opnamestream is genoeg). De MIDI-code van de driver hangt daar niet van
  af; het apparaat wel.

## Bekende beperkingen

- **MIDI alleen terwijl audio loopt.** Zie "Gemeten met deze driver"; met
  het WirePlumber-profiel blijft de stream open, dus blijft MIDI werken.
- **Schakelaars moeten kloppen.** De Aux/Phono-schakelaar voor Ch.1/2 en
  Ch.3/4 moet passen bij de opnamebron die in het configuratiescherm is
  gekozen; de driver kan de schakelaars niet lezen.
- **Een vergrendelde S/PDIF-ingang bepaalt de klok.** Als de S/PDIF-ingang
  vergrendeld is, volgt het apparaat die klok, ook als de interne klok is
  gekozen. Het configuratiescherm toont de lock-status.
- **S/PDIF-audio** (opnemen van of afspelen naar S/PDIF) is nog niet in
  kaart gebracht.
- **Gain en 48 V zijn analoog,** op het apparaat zelf; niet via software
  te regelen.
- **`status read failed: bRequest=0x49`** in `dmesg` (alleen op
  debugniveau) is verwacht en onschadelijk - zie
  [Als iets niet werkt](linux/README.nl.md#als-iets-niet-werkt).
- **De originele Windows-skin is optioneel** en moet uit je eigen kopie
  van het driverpakket van de fabrikant worden geïmporteerd; hij zit hier
  niet bij.

## Disclaimer

Dit project is niet verbonden aan en wordt niet ondersteund door
TerraTec, TerraSoniq, Musonik of Ploytec. Alle handelsmerken zijn van hun
eigenaren. De driver is ontwikkeld door onafhankelijke reverse
engineering voor interoperabiliteit; er zit geen code of firmware van de
fabrikant in. Het is een experimentele kernelmodule, zonder garantie
(zie de GPL-2.0-licentie).

## Dankwoord

- De ontwikkelaars van ALSA en de Linux-kernel.
- De oorspronkelijke engineers van de PHASE X64 en zijn firmware.
- Marcel Bierling, voor de [Ozzy](https://github.com/mischa85/Ozzy)-driver
  voor Ploytec-interfaces (MIT), een vroege referentie voor het protocol.
- De gebruikers en reviewers wier gepubliceerde reviews en discussies de
  hardwarebeschrijving hebben gevoed:
  [Audiofanzine](https://en.audiofanzine.com/usb-interface/terratec/terrasoniq-phase-x64-usb/user_reviews/),
  de [KVR Audio-forumdraad](https://www.kvraudio.com/forum/viewtopic.php?t=309484).
- AI-agents die hebben bijgedragen:
  - Claude van Anthropic (Opus en Sonnet) - analyse, implementatie van
    driver en configuratiescherm, reviews.
  - OpenAI Codex - de eerste implementatie van de MIDI-monitor.
- Projectleider: Flark / flarkAUDIO (GitHub
  [flarkflarkflark](https://github.com/flarkflarkflark)) - richting en al
  het testen op hardware.

Built by loopback-coding: AI-assisted, human-directed, hardware-verified.
Every change went out, came back through real hardware, and was measured
before it counted.

## Licentie

GPL-2.0-only - zie [LICENSE](LICENSE) - behalve de foto's in
[docs/hardware/](docs/hardware/README.md), die © 2026 flarkflarkflark
zijn en vallen onder
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.nl). De
reverse-engineeringnotities staan in [analysis/](analysis/README.md).
