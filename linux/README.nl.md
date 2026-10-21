# PHASE X64 USB onder Linux

> Vertaling van `README.md`, dat leidend is. Bij verschil geldt de Engelse
> tekst.

Linux-ondersteuning voor de TerraTec / TerraSoniq PHASE X64 USB (USB-ID
`0a4a:5b01`). Het protocol is uit de Windows-driver 2.9.34 gehaald; zie
`../analysis/NOTES.md`.

- `snd-usb-phasex64/`: ALSA-kerneldriver (afspelen, opnemen, MIDI, mixer)
- `phasex64-panel/`: configuratiescherm, de Linux-versie van `CPLPhaseX64USB.exe`

**Versies:** de driver, het configuratiescherm en de pakketten delen één
versienummer. Dat sprong van 0.1.13 naar 0.9.0 voor de eerste publieke
release (het configuratiescherm had daarvoor een eigen nummer, 0.2.0); de
opmerkingen "Sinds v0.1.12" / "Sinds v0.1.13" hieronder verwijzen naar die
eerdere driverversies.

## Wat de driver levert

| ALSA | Inhoud |
|---|---|
| PCM afspelen | 4 kanalen, S24_3LE, 44,1 / 48 / 88,2 / 96 kHz |
| PCM opnemen | 6 kanalen, S32_LE (24 bit), zelfde rates |
| MIDI | 1 in, 1 uit |
| Mixer | alle knoppen van het Windows-paneel, plus "Clock Source" en "Digital Input Lock" |

Afspelen en opnemen draaien altijd op dezelfde samplerate (één klok in het
apparaat). **Geteste samplerates:** 48 kHz is de samplerate van dagelijks gebruik
en van de functietests sinds driver v0.1.10 (live wijzigingen, MIDI,
PipeWire/REAPER, buffergroottes). 44,1, 88,2 en 96 kHz zijn één
keer op hardware getest, met v0.1.10 op 2026-10-14 (direct via ALSA,
zonder PipeWire): per samplerate 12 s afspelen en opnemen met een toon van
1 kHz via een analoge loopback, realtime, zonder fouten, plus wisselen van
samplerate zonder opnieuw in te pluggen (`tests/hw/REPORT.md`,
"Acceptance tests (2026-10-14...)"); 44,1 kHz ook in de
bit-exactheidstest van opnamekanalen 5/6
(`../analysis/loopback-and-monitoring.md`). Ze zijn niet opnieuw getest met
de latere functies of via PipeWire.

MIDI werkt alleen zolang er een audiostream (afspelen of opnemen) actief is
- bijvoorbeeld met REAPER open en een lopende stream, of `arecord`/`aplay`
tegen de kaart - zelfde beperking als de originele Windows-driver, en
bevestigd op echte hardware: MIDI stopt zodra de stream stopt, het blijft
niet "gewapend" staan (zie `../analysis/NOTES.md`, "MIDI").

## Installeren op Arch Linux

```sh
sudo pacman -S --needed base-devel dkms linux-headers   # of linux-zen-headers enz.
cd linux
makepkg -s
sudo pacman -U snd-usb-phasex64-dkms-*.pkg.tar.zst phasex64-panel-*.pkg.tar.zst
```

`makepkg -s` bouwt alle drie de pakketten; installeer de driver en het
configuratiescherm zoals hierboven. Het derde, `phasex64-wireplumber`, is
optioneel en verandert het gedrag van WirePlumber voor de hele machine -
lees eerst "PipeWire / JACK" hieronder. (`makepkg -si` zou alle drie in
één keer installeren, ook dat pakket.)

DKMS bouwt de driver, ook na elke kernelupdate. Daarna de kaart uit- en
weer inpluggen, of:

```sh
sudo modprobe snd-usb-phasex64
```

Handmatig bouwen zonder pakket kan ook:

```sh
cd linux/snd-usb-phasex64
make
sudo insmod snd-usb-phasex64.ko
```

## Configuratiescherm

De originele skin zit niet in dit project. Importeer die één keer uit het
Windows-driverpakket (bijvoorbeeld vanaf de Windows-partitie):

```sh
phasex64-panel --import-skin /pad/naar/MUSONIK_PHASE_X64_USB_X64_2.9.34
phasex64-panel
```

Zonder skin (`phasex64-panel --plain`) krijg je gewone knoppen met dezelfde
functies. Alle knoppen zijn ook gewone ALSA-controls
(`alsamixer -c PhaseX64`) en worden met `alsactl store` bewaard. De
latency-knoppen van Windows zijn vervangen door de BUFFER-regeling van
het configuratiescherm, die de buffergrootte van PipeWire instelt (zie
"Buffergrootte en latentie"); stel bij JACK2 of directe ALSA-toegang de
buffer daar of in je DAW in.

De panel-UI is meertalig via gettext en volgt de systeemtaal
(`LANG`/`LC_MESSAGES`/`LC_ALL`, die bijvoorbeeld Plasma instelt). Zie
`phasex64-panel/po/README.md` voor de vertaalworkflow - Nederlands (`nl`)
is de eerste vertaling.

**Let op bij upgraden naar v0.1.4**: de vijf losse "In 1/2 ... Capture
Switch"-controls en de vier "In 3/4 ... Capture Switch"-controls zijn
vervangen door twee keuzelijsten, `In 1/2 Capture Source` en `In 3/4
Capture Source` (zie `analysis/NOTES.md`, "ALSA control names"). Een
oude `/var/lib/alsa/asound.state` met de oude namen matcht die controls
niet meer — onschuldig, maar draai na de eerste keer laden van v0.1.4
wel opnieuw `sudo alsactl store` zodat de nieuwe namen bewaard worden.

**Sinds v0.1.12**: panelwijzigingen (codec-routing, RIAA, bronkeuze)
worden live toegepast terwijl een stream loopt — afspelen/opnemen gaan
door, zonder hoorbaar gat in de tijdlijn van de stream (gemeten drift
onder 3ms per wijziging, niet cumulatief). De wijziging zelf vindt
plaats tijdens een korte (~0,3s) interne herconfiguratie waarbij er
geen geluid is; één klik of transiënt die precies in dat venster valt
kan daarbij verloren gaan. Vóór v0.1.12 werd een wijziging tijdens het
streamen pas toegepast bij de volgende keer starten — het panel
toont die oudere tekst nog wanneer het geladen is tegen een driver
ouder dan v0.1.12.

## MIDI-monitor

Twee IN/OUT-activiteitsleds staan naast het `MIDI:`-veld van de
statusbalk, altijd aan - ze lichten kort op bij echt verkeer
(aangestuurd uitsluitend door de ALSA-rawmidi `Rx`/`Tx`-bytetellers in
`/proc/asound/cardN/midiN`, gelezen bij de normale poll), ongeacht of
het monitorvenster hieronder open is, en ongeacht de PCM-streamstatus.
Het `MIDI:`-veld van de statusbalk zelf toont
`beschikbaar`/`niet beschikbaar` - of het rawmidi-*apparaat* aanwezig
is - niet de verbindingsstatus van het monitorvenster zelf (die staat
alleen in het venster zelf, rechtsboven).

Een knop **MIDI…** naast **CLOCK…**/**OVER** opent een thematisch
venster dat binnenkomende MIDI-berichten als tekst toont. Alleen
zodra dat venster open is, zoekt het panel de PHASE ALSA-seq-poort op
naam op (nooit een vast clientnummer - elke keer opnieuw opgezocht,
zodat hernummering van kaart/client en loskoppelen/opnieuw aansluiten
geen herstart nodig hebben) en draait het `aseqdump` daartegen; het
venster sluiten stopt die `aseqdump` netjes (`SIGINT`, geen kille
kill, zodat die eerst nog doorspoelt) - er draait niets op de
achtergrond terwijl het venster dicht is.

**Alleen IN, geen OUT**: het venster toont binnenkomende berichten als
tekst, maar uitgaande berichten (alles wat je DAW of een andere app
*naar* het apparaat stuurt) kunnen alleen als activiteit (de OUT-led)
getoond worden, nooit als berichtinhoud - ALSA's sequencer heeft geen
passieve manier om data te observeren die een andere client in een
poort schrijft, alleen wat de poort zelf naar zijn eigen abonnees
uitzendt. Dit is een structurele beperking, niet iets wat een
toekomstige versie van dit panel kan opheffen.

**Heeft een actieve stream nodig om daadwerkelijk iets te doen**: de
leds, de knop en het monitorvenster werken allemaal ongeacht de
PCM-streamstatus - maar er stromen alleen echte MIDI-bytes zolang er
een stream actief is. Dit is het eigen gedrag van de hardware
(herhaaldelijk bevestigd, ook met een echte externe controller - zie
`../analysis/NOTES.md`, "MIDI"), niet iets wat deze driver afdwingt of
kan opheffen: zonder lopende stream blijft het monitorvenster stil
zonder dat er iets binnenkomt, en de eigen hint-tekst zegt dat ook -
dat is verwacht, geen teken dat er iets kapot is. Zie "Wat de driver
levert" hierboven voor dezelfde beperking vanuit de driver zelf.

**OUT problemen oplossen**: rechtstreeks naar de PHASE ALSA-seq-poort
sturen (bijv. `aseqsend`) bereikt de hardware alleen zolang een andere
toepassing de rawmidi-*uitgang* van het apparaat al open heeft (REAPER
via PipeWire's Midi-Bridge, `aplay`, enz. - controleer `Owner PID`
onder `Output 0` in `/proc/asound/cardN/midi0`). Zonder eigenaar wordt
het bericht stilletjes weggegooid en bewegen de Tx-teller/OUT-led
simpelweg niet - dit is gedrag van de ALSA-sequencer/rawmidi-bridge,
niet iets wat deze driver of dit panel beïnvloedt.

Clock- en Active Sensing-berichten worden standaard uit de monitor
gefilterd (een stabiele klok van 120 BPM alleen al is 48
berichten/seconde - meestal ruis voor iemand die het logboek leest),
met een schakelaar om ze te tonen. Het eigen verkeer van andere
ALSA-clients die verbinden/loskoppelen (bijv. "Port subscribed") wordt
apart gefilterd, ook standaard uit, via een eigen schakelaar
"Systeemgebeurtenissen tonen" - een ander soort ruis dan Clock/Active
Sensing, de moeite waard om er onafhankelijk van te kunnen tonen.
Lange regels (bijv. een SysEx-dump) lopen door op de volgende regel in
plaats van afgekapt te worden aan de rand van het venster; de
geschiedenis is begrensd en het scherm werkt bijgewerkt in batches,
niet één keer per binnenkomend bericht.

## Eerste test

```sh
dmesg | grep -i phase                      # driver geladen, geen fouten?
aplay -l; arecord -l                       # kaart zichtbaar?
speaker-test -D hw:PhaseX64 -c 4 -F S24_3LE -r 48000 -t sine
arecord -D hw:PhaseX64 -c 6 -f S32_LE -r 48000 -d 5 test.wav
```

Daarna in PipeWire/REAPER gewoon de kaart "PHASE X64 USB" kiezen.

## PipeWire / JACK

Standaard kiest PipeWire/WirePlumber voor deze kaart mogelijk een
consumenten-stereoprofiel (waardoor de meeste kanalen verborgen
blijven) in plaats van "Pro Audio", en REAPER's eigen exclusieve
ALSA-toegang betekent dan dat PipeWire eerst gestopt moet worden.
`linux/wireplumber/51-phasex64-pro-audio.conf` voorkomt beide: het
dwingt het "Pro Audio"-profiel af en zet de twee nodes (4-kanaals
afspelen, 6-kanaals opnemen) vast op de native 48kHz-samplerate
zonder resampling of kanaalmixing, zodat REAPER het apparaat **via**
PipeWire kan gebruiken (via `pipewire-jack`), tegelijk met gewone
desktopaudio, zonder dat PipeWire ooit gestopt hoeft te worden.

**Installeren (eerst testen, per gebruiker)**:

```sh
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp linux/wireplumber/51-phasex64-pro-audio.conf ~/.config/wireplumber/wireplumber.conf.d/
systemctl --user restart wireplumber
```

**Controleren**:

```sh
wpctl status                 # Active Profile: pro-audio bij de kaart;
                              # standaard sink/source (*) moet PHASE X64 zijn
pw-dump | grep -i phase -A3 -B3 | grep -i 'audio.rate\|audio.format'
```

Verwacht `audio.rate = 48000` zonder andere aangeboden samplerate, en
`audio.format` zoals ACP dat al per node onderhandelt (`S24LE` bij
afspelen, `S32LE` bij opnemen - deze configuratie legt het formaat
bewust niet vast, zie het commentaar in het `.conf`-bestand zelf: de
twee nodes hebben verschillende waarden nodig, en ACP doet dat al
correct op eigen kracht).

**Eenmalige volumefix**: de opname-node kan de *eerste* keer dat
WirePlumber hem ooit ziet opstarten met een softwarevolume onder unity
(70% waargenomen) - dit apparaat heeft geen hardware-opnamevolume om
uit te herstellen (`amixer -c PhaseX64` heeft geen volumeregeling), en
niets in deze configuratie kan het volume van een node bij aanmaak
vastzetten. Niet bit-exact bij 70% - eenmalig verhelpen:

```sh
pactl set-sink-volume alsa_output.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-output-0 100%
pactl set-source-volume alsa_input.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-input-0 100%
```

WirePlumber onthoudt dit (`~/.local/state/wireplumber/stream-properties`)
en herstelt het bij elke volgende herstart/herverbinding - een
eenmalige fix per machine, geen terugkerende stap.

Eenmaal bevestigd, installeer het systeembreed voor elke gebruiker op
de machine via het optionele pakket `phasex64-wireplumber`,
dat `makepkg -s` in deze map `linux/` samen met de driver en het
configuratiescherm bouwt (zie "Installeren op Arch Linux" hierboven):

```sh
sudo pacman -U phasex64-wireplumber-*.pkg.tar.zst
```

Dit is een apart, optioneel pakket in plaats van onderdeel van
`phasex64-panel` - zie het commentaar bij
`package_phasex64-wireplumber()` in de PKGBUILD voor waarom (het
verandert het gedrag van de sessiemanager voor de hele machine, niet
alleen voor wie de GUI-mixer wil). Het installeert hetzelfde bestand
naar `/usr/share/wireplumber/wireplumber.conf.d/51-phasex64-pro-audio.conf`
- **verwijder eerst je testkopie op gebruikersniveau** (het
`rm`-commando onder "Rollback" hieronder, niet de
`systemctl restart`) zodat WirePlumber dezelfde regel niet dubbel
vanuit twee plekken laadt; herstart daarna, ná installatie van het
pakket, eenmalig `systemctl --user restart wireplumber`.

**Poortnamen**: de poorten van het "Pro Audio"-profiel heten generiek
per kanaalindex (`playback_AUX0`..`AUX3`, `capture_AUX0`..`AUX5`),
overeenkomstig de kanaalvolgorde van deze driver (afspelen: Out 1/2,
Out 3/4; opnemen: In 1/2, In 3/4, Out 1/2-aftak - zie "Opnemen wat je
hoort" hieronder). Ze hernoemen is niet mogelijk vanuit dit
configuratiebestand - poortnamen komen uit de ALSA-Card-Profile
profile-set zelf, niet uit een WirePlumber-regel - hetzelfde onderliggende
probleem als opnamekanalen 5/6 die geen herkenbaar label hebben in
gewone ALSA, één laag verder naar buiten; zie
`analysis/loopback-and-monitoring.md`, "Proposed exposure", waar dit
wordt bijgehouden.

**Desktopaudio**: een gewone stereo-app (browser, mediaspeler) komt
alsnog op Out 1/2 terecht met de juiste L/R - `channelmix.disable`/
`resample.disable` hierboven leggen alleen de grens van de *node* met
de hardware vast (er is niets meer om te converteren, want
formaat/samplerate komen al native overeen); het routeren van een
gewone 2-kanaals clientstream naar de eerste twee van de vier kanalen
van deze node is een apart onderdeel van PipeWire's eigen standaard
stream-naar-sink-mixing, onafhankelijk van die twee eigenschappen.

**Bit-exactheid controleren**: speel een bekend 24-bit-bestand via
PipeWire af naar Out 1/2, neem de opname-node van de kaart op, en
vergelijk opnamekanalen 5/6 (de bit-exacte Out 1/2-aftak, dezelfde als
bij "Opnemen wat je hoort" hieronder) met het bronbestand - elk
verschil betekent dat PipeWire onderweg iets heeft geconverteerd, en
moeten de rate/formaat-instellingen hierboven opnieuw gecontroleerd
worden. **Bevestigd** (2026-10-19, toon van 1kHz/-12dBFS,
`pw-play`/`pw-record` met `--channel-map AUX0,AUX1,AUX2,AUX3,AUX4,AUX5`):
140000/140000 samples op beide kanalen bit-voor-bit identiek aan het
bronbestand, geen enkel verschil - *mits het volume van de sink
precies op 100% staat* (zie volgende paragraaf; de eerste poging tot
deze test, bij het toenmalige volume van 95% van de sink, reproduceerde
precies dat volume als een uniforme lineaire schaalfactor met een
restfout van minder dan 1 LSB - geen PipeWire-conversieartefact, maar
gewoon de normale softwarevolumeregeling die, terecht, nog steeds in
het signaalpad zit).

**Volume van sink/source moet op 100% blijven voor bit-exact
gebruik**: het eigen softwarevolume van de PHASE X64-sink
(`wpctl`/`pactl`/de desktop-volumeregeling) schaalt *alles* dat naar
Out 1/2 gestuurd wordt - desktopaudio, REAPER/JACK-afspelen, alles -
en het softwarevolume van de source schaalt alles wat opgenomen wordt
op dezelfde manier. Dit is normaal, verwacht PipeWire-gedrag (zie
"Eenmalige volumefix" hierboven) en niet iets wat deze configuratie
uitschakelt, want desktopgebruikers hebben nog steeds een werkende
volumeregeling nodig - maar het betekent dat bit-exact opnemen/afspelen
via PipeWire vereist dat beide eerst precies op 100% gezet worden:

```sh
pactl set-sink-volume alsa_output.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-output-0 100%
pactl set-source-volume alsa_input.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-input-0 100%
```

**REAPER via JACK**: vereist `pipewire-jack` (`sudo pacman -S
pipewire-jack`, niet standaard geïnstalleerd). Zet REAPER's audiosysteem
op JACK; pipewire-jack koppelt dat aan dezelfde PipeWire-graaf als al
het andere. Latency/quantum is een **graafbrede** PipeWire-instelling,
niet specifiek voor dit apparaat - stel die in met
`pw-metadata -n settings 0 clock.force-quantum <256|1024|0>` (`0` wist
de override) of de BUFFER-regeling van het configuratiescherm (zie
"Buffergrootte en latentie" hieronder) in plaats van dit bestand aan te
passen. **Bevestigd**
(2026-10-19): REAPER via `pipewire-jack` bij quantum 1024 en 256,
6-kanaals/4-kanaals, continu opnemen van vinyl op In 1/2, desktopaudio
ernaast blijft werken, geen hapering.

**Live toepassen tijdens streamen via PipeWire**: het ~0,3s-herstart-
gat van een panelwijziging (zie "Sinds v0.1.12" hierboven) gebeurt
onder PipeWire op dezelfde manier als bij rechtstreekse ALSA-toegang -
PipeWire doorstaat dat via hetzelfde tijdlijn-opvulmechanisme, zonder
de node te laten vallen. **Bevestigd** (2026-10-19): live
panelwijzigingen op In 1/2 tijdens een REAPER/JACK-opname bij zowel
quantum 1024 als 256 - opname bleef continu, geen node die wegvalt,
geen hapering.

**Suspend**: standaard sluit WirePlumber een inactief ALSA-apparaat na
5s (`session.suspend-timeout-seconds`). De configuratie hierboven zet
dat voor deze kaart uit (`= 0`) zodat MIDI - die deze driver alleen
doorgeeft terwijl er een PCM-stream open is, zie hierboven - altijd
blijft werken in plaats van alleen vlak na het laatst afgespeelde
geluid. Afweging: de hardware van het apparaat zelf (klok, DAC/ADC)
schakelt zichzelf dan nooit meer uit. Zet de waarde terug op `5`, of
verwijder die regel, om het standaard inactief-sluiten-gedrag terug te
krijgen.

**Rollback**: het bestand verwijderen en wireplumber herstarten
maakt het *suspend*-gedrag direct ongedaan - zonder het bestand gaan
de nodes weer in suspend bij inactiviteit (het panel toont "Stream:
idle", lege meters, "MIDI: inactive" totdat er een stream start, in
plaats van altijd actief) - **maar niet het profiel** (bevestigd
2026-10-19): WirePlumber onthoudt het laatst gebruikte profiel in zijn
eigen state (`~/.local/state/wireplumber/default-profile`), los van of
dit configuratiebestand bestaat. Als `pro-audio` ooit actief is
geweest, blijft het actief na het verwijderen van het bestand, want
WirePlumber herstelt *dat*, niet wat ACP vanaf nul zou kiezen. Een
volledige terugkeer naar het gedrag van vandaag vereist beide stappen:

```sh
rm ~/.config/wireplumber/wireplumber.conf.d/51-phasex64-pro-audio.conf
systemctl --user restart wireplumber
pactl set-card-profile alsa_card.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00 \
  output:multichannel-output+input:multichannel-input
```

(of kies een profiel via de geluidsinstellingen van Plasma/je
desktopomgeving in plaats van het `pactl`-commando - zelfde effect).
Sla die laatste stap over als je `pro-audio` wilt blijven gebruiken
zonder de rest van deze configuratie (suspend, rate/formaat-pinning) -
het profiel en dit bestand staan los van elkaar, en dat is precies
waarom het bestand alleen verwijderen het profiel niet terugdraait.

## Buffergrootte en latentie

PipeWire verwerkt audio in blokken van een vast aantal samples, het
*quantum*. Dat is één instelling voor de hele PipeWire-graaf, niet per
apparaat: kleiner betekent minder latentie en meer CPU-belasting. Eén
blok duurt quantum / samplerate - 64 samples bij 48 kHz is 1,3 ms, 1024
is 21,3 ms. Dat is één periode; de totale rondgang door de interface is
langer.

**In het configuratiescherm**: BUFFER, naast de presets, biedt Auto en
64 tot 2048. Een wijziging wordt live toegepast, en het label ernaast
toont de bloklengte bij de huidige samplerate; bij Auto toont het de
grootte waarmee PipeWire werkelijk draait, als die te lezen is. De
regeling vereist een draaiende PipeWire. Stel bij JACK2 de buffer in
JACK in; bij directe ALSA-toegang kiest de applicatie hem zelf.

**Bewaren na herstart** schrijft daarnaast
`~/.config/pipewire/pipewire.conf.d/20-phasex64-buffer.conf`, waarmee de
grootte de *standaard* van PipeWire wordt voor nieuwe sessies na een
herstart van PipeWire (zo nodig wordt ook `default.clock.min-quantum`
verlaagd; andere configuratiebestanden worden nooit aangeraakt; het
vinkje weghalen verwijdert het bestand). Omdat het een standaard is en
geen afgedwongen waarde, kan een applicatie PipeWire nog steeds om een
grotere buffer vragen.

**Zonder het configuratiescherm**:

```sh
pw-metadata -n settings 0 clock.force-quantum 128   # live, tot PipeWire herstart
pw-metadata -n settings 0 clock.force-quantum 0     # terug naar Auto
PIPEWIRE_QUANTUM=128/48000 reaper                   # alleen voor één applicatie
```

of een eigen drop-in-bestand in `~/.config/pipewire/pipewire.conf.d/`
met `context.properties = { default.clock.quantum = 128 }`, gevolgd door
`systemctl --user restart pipewire pipewire-pulse wireplumber`.

**Getest** (2026-10-21, REAPER via `pipewire-jack`, tegelijk afspelen en
opnemen): 64, 128, 256 en 1024 draaien stabiel. Een live wijziging kan
een korte klik geven - de ERR-teller van `pw-top` steeg met 1-2 bij elke
wissel, niet tijdens het draaien; bij 64 gebruikte de graaf ongeveer 7%
van zijn tijdbudget (W/Q ≈ 0,07). 512 en 2048 zijn niet getest maar
liggen binnen dat bereik; groottes onder 64 worden niet aangeboden. De
titelbalk van REAPER toont een nieuwe grootte pas nadat het afspelen is
gestopt en opnieuw gestart.

**Opname-offset in REAPER**: bij 64 kwam een analoge loopback-opname
(Out 3/4 naar Aux In 3/4) ongeveer 54 samples (≈1,1 ms) later binnen
dan REAPER's eigen latentiecompensatie verrekent. Dat is een eerste,
benaderende meting. Neem voor sample-nauwkeurige overdubs één keer een
korte loopback op, meet het verschil en vul dat in als REAPER's
handmatige opname-offset (Preferences → Audio → Recording).

## Indeling opnamekanalen

Het apparaat neemt altijd 6 kanalen op in één stream:

| kanalen | inhoud |
|---|---|
| 1/2 | In 1/2 - wat `In 1/2 Capture Source` ook kiest |
| 3/4 | In 3/4 - wat `In 3/4 Capture Source` ook kiest |
| 5/6 | Out 1/2-aftak - altijd actief, digitaal, bit-exacte kopie van de PCM/DSP-data die naar Out 1/2 gestuurd wordt, vóór enige uitgangsmixing (zie "Opnemen wat je hoort" hieronder) |

Twee van de keuzes van `In 1/2`/`In 3/4 Capture Source`, `Loopback 3/4`
en `Loopback 1/2`, zijn een **ander mechanisme** dan opnamekanalen 5/6
hierboven, makkelijk ermee te verwarren:

| | mechanisme | komt terecht op | incl. Direct |
|---|---|---|---|
| Opname 5/6 | digitaal, bit-exacte aftak van de PCM/DSP-data naar Out 1/2 | kanalen 5/6, altijd | nee - vóór uitgangsmixing |
| `Loopback 3/4`/`Loopback 1/2` | analoog, de eigen analoge uitgang van het *andere* uitgangspaar | kanalen 1-4, welk paar je ook kiest | ja |

Zet `In 1/2 Capture Source = Loopback 3/4` en kanalen 1/2 leggen Out
3/4's werkelijke analoge uitgang vast (hardware-bevestigd, 2026-10-19 -
zie `analysis/loopback-and-monitoring.md` voor de volledige test); `In
3/4 Capture Source = Loopback 1/2` is de spiegel voor Out 1/2 naar In
3/4 (op dezelfde manier aangenomen door de identieke vendor-naamgeving,
niet onafhankelijk getest). Beide liggen ongeveer 0,3-0,8 dB lager dan
de werkelijke uitgang en zakken naar de analoge ruisvloer (~-85 dBFS)
wanneer die uitgang stil is - geen digitale kopie.

**Waarschuwing voor feedbacklussen (software-monitoring)**: een track
monitoren waarvan de input een Loopback-bron is, terug naar diezelfde
uitgang, is een feedbacklus - schakel de inputmonitoring van die track
uit. Het panel toont hiervan een live herinnering in het eigen gebied
van die ingang zodra een Loopback-bron daadwerkelijk geselecteerd is.

**Waarschuwing voor feedbacklussen (analoog, hardware-bevestigd
2026-10-19)**: er bestaat een *andere*, puur analoge lus als je
**beide** Loopback-bronnen selecteert **en** Direct aanzet op **beide**
uitgangen tegelijk - In 1/2 -> Direct -> Out 1/2 -> `Loopback 1/2` ->
In 3/4 -> Direct -> Out 3/4 -> `Loopback 3/4` -> In 1/2, volledig
gesloten binnen de kaart zelf, zonder enige DAW/software-monitoring.
Gemeten: beide ingangen stijgen naar ongeveer -68,7 dBFS (t.o.v. de
ruisvloer van ~-85 dBFS met de lus open) - bij unity gain is het
alleen ruis, maar het kan gaan rinkelen/oscilleren als het gain ergens
in die lus omhoog gaat. Vermijd dit door Direct uit te zetten op ten
minste één van de twee uitgangen, of door maar één Loopback-bron
tegelijk te gebruiken. Het panel schakelt beide live-indicatoren naar
een amberkleurige waarschuwing zodra alle vier de voorwaarden kloppen,
en weer terug zodra dat niet meer zo is. Zie
`analysis/loopback-and-monitoring.md` voor de volledige meting.

**In PipeWire** heten de poorten generiek per kanaalindex
(`capture_AUX0`..`AUX5`, `playback_AUX0`..`AUX3` - zie "PipeWire /
JACK" hierboven), niet volgens deze indeling, en de WirePlumber-
configuratie in deze repo kan ze niet hernoemen - dezelfde onderliggende
beperking als opnamekanalen 5/6 die geen eigen ALSA-control hebben,
bijgehouden in `analysis/loopback-and-monitoring.md`, "Proposed
exposure".

## Frontingangen (Mic / Hi-Z / Line)

Met `In 1/2 Capture Source = Mic-HiZ-Line`: Ch.1 is de combo-aansluiting
aan de voorkant (XLR + 6,3mm jack, met eigen gain-knop en +48V) en komt
op opnamekanaal 1 (links) terecht; Ch.2 is de Hi-Z/Line-jack aan de
voorkant (eigen gain-knop) en komt op kanaal 2 (rechts) terecht - beide
mono. De schakelaars aan de zijkant kiezen Line/Mic voor Ch.1 en
Line/HiZ voor Ch.2.

`Direct` op Out 1/2 houdt ze gescheiden (mic links, Hi-Z/Line rechts);
`Direct Mono` (alleen te selecteren met deze bron, en alleen als Direct
aan staat) telt beide kanalen bij elkaar op naar beide kanten.

**+48V staat alleen op de XLR-pin van Ch.1** - nooit op de jack van
Ch.2, en ook niet op de jack-helft van Ch.1 zelf. De schakelaar van
Ch.1 selecteert feitelijk welke *aansluiting* actief is, niet alleen een
gain-trap: met de schakelaar op Mic geeft een jack in de combo-
aansluiting niets (alleen preamp-ruis) - zet hem op Line in plaats
daarvan.

Alle gain (en +48V) wordt op het apparaat zelf ingesteld, vóór de ADC -
noch de driver, noch het paneel kan dit lezen of regelen. Het Clip-LED-
je gaat branden bij 0dBFS op de ADC. Typische instellingen:
condensatormicrofoon -> 48V + schakelaar op Mic, gain laag en opbouwen;
passieve bas/gitaar -> HiZ; synth of andere line-level apparatuur ->
Line. Gemeten gain-bereiken en ruwe getallen staan in
`analysis/NOTES.md`, "Front inputs: Mic / Hi-Z / Line".

## Opnemen wat je hoort

Als PipeWire het apparaat beheert (het normale geval): neem op vanaf de
eigen PipeWire-monitorbron van de kaart - geen extra instellingen nodig:

```sh
pactl list sources short | grep monitor      # zoek "...PhaseX64...monitor"
```

Heeft REAPER (of een andere app) het apparaat rechtstreeks in beheer, en
wil je **Out 1/2** opnemen: opnamekanalen 5/6 zitten altijd in de normale
6-kanaals opnamestream en zijn een bit-exacte digitale kopie van wat er
naar Out 1/2 gestuurd wordt - geen kabel, geen extra routing nodig. Voeg
in REAPER ingangskanalen 5/6 toe aan een track, zet 'm op record. Let op:
5/6 volgt wat er naar Out 1/2 *gestuurd* wordt, niet per se wat daar op
dat moment hoorbaar is - het blijft opnemen ook als `PCM 1/2 Playback
Switch` uit staat. Voor Out 3/4, of voor exclusieve-toegang-opstellingen
waar echt een hardware-loopback nodig is, zie
`../analysis/loopback-and-monitoring.md`.

## Instellingen bewaren

Routerings-/bronschakelaars, RIAA en Clock Source worden automatisch
hersteld: `alsa-restore.service` draait `alsactl restore` zodra het
apparaat verschijnt (na de eigen opstartstandaarden van de driver), en
draait `alsactl store` automatisch bij een normale uitschakeling of
herstart - geen handmatige stap nodig in het normale geval. Live/alleen-
lezen status - Digital Input Lock, Digital Input Rate en de vier
piekmeters - wordt nooit hersteld; die weerspiegelt altijd direct de
hardware.

Als de machine crasht of stroom verliest vóór een normale uitschakeling,
gaat alles verloren wat je sinds de laatste opslag hebt gewijzigd. Draai
`sudo alsactl store` na het wijzigen van instellingen als je zeker wilt
zijn dat ze die situatie overleven.

De presets van het paneel (menu Presets) zijn iets anders: zelf opgeslagen
en geladen configuraties op naam, geen opstartmechanisme - ze vervangen
`alsactl store`/`restore` niet.

## Als iets niet werkt

Stuur de uitvoer van `dmesg` na het inpluggen en na een afspeel- of
opnamepoging.

Een incidentele `status read failed: bRequest=0x49 ... err=-32` in
`dmesg` is verwacht en onschadelijk - zie `../analysis/NOTES.md`,
"0x49 status-read stalls". Sinds v0.1.13 verschijnt dit bericht niet
meer op foutniveau bij normaal gebruik; het is alleen zichtbaar met
dynamic debug ingeschakeld op `snd_usb_phasex64`. Het wordt gelogd op
naam van het USB-apparaat, niet de ALSA-kaartnaam, dus
`dmesg | grep -i phasex64` alleen vindt het niet - grep in plaats
daarvan op `dmesg | grep -iE "status read|0x49"`.

## Licentie

De driver, het configuratiescherm en al het overige in deze repository
vallen onder de GNU General Public License, alleen versie 2
(`GPL-2.0-only`) - zie `../LICENSE`.
