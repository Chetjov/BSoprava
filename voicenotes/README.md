# voicenotes

Hlasová poznámka z iPhonu skončí jako markdown ve vaultu. Všechno lokálně,
audio nikam ven neodchází.

```
iPhone (Shortcut)
   │  POST multipart/form-data, hlavička X-Auth-Token
   ▼
Pi 4 — GATEWAY          uloží .m4a do fronty, vrátí 200, nic víc
   ▼
Notebook — WORKER       rsync fronty → (přepis) → zápis .md
   ▼
Obsidian vault → Syncthing → iPhone
```

Zachycení je nezávislé na zpracování: telefon nikdy nečeká, jestli je notebook
zapnutý. Když je vypnutý, fronta na Pi se plní a worker to dožene.

## Stav

| Fáze | Obsah | Stav |
|---|---|---|
| 1 | Transport: gateway, worker, poznámka s placeholderem | **hotovo** |
| 2 | Přepis (faster-whisper, VAD, chybové stavy) | čeká |
| 3 | Strukturování (ollama / anthropic, uzavřené tagy) | čeká |
| 4 | Benchmark modelů + týdenní review skript | čeká |

Fáze 1 je celá cesta telefon → vault bez modelů. Poznámka vznikne s
`status: needs-review` a placeholderem místo přepisu — jde o ověření
transportu, ne o užitečný obsah.

## Instalace — Pi 4 (gateway)

```bash
sudo useradd --system --home /srv/voicenotes --shell /usr/sbin/nologin voicenotes
sudo mkdir -p /srv/voicenotes /etc/voicenotes /opt/voicenotes
sudo chown -R voicenotes:voicenotes /srv/voicenotes

sudo python3 -m venv /opt/voicenotes/venv
sudo /opt/voicenotes/venv/bin/pip install '/cesta/k/voicenotes[gateway]'

sudo cp config.example.yaml /etc/voicenotes/config.yaml
sudo -e /etc/voicenotes/config.yaml          # zkontroluj root, host, port
```

Token do souboru s konfigurací nepatří, jde do prostředí služby:

```bash
printf 'VOICENOTES_TOKEN=%s\n' "$(openssl rand -hex 24)" \
  | sudo tee /etc/voicenotes/gateway.env > /dev/null
sudo chmod 600 /etc/voicenotes/gateway.env
sudo chown root:voicenotes /etc/voicenotes/gateway.env
```

**Časová zóna Pi musí sedět na `worker.timezone`** — Pi razítkuje názvy
souborů lokálním časem a worker z nich skládá `created:`. Když se rozejdou,
worker to při každém běhu ohlásí do žurnálu, ale radši rovnou:

```bash
sudo timedatectl set-timezone Europe/Prague
```

Spuštění:

```bash
sudo cp systemd/voicenotes-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voicenotes-gateway
curl -s http://<pi-tailscale>:8080/health      # {"status":"ok","queued":0}
```

Endpoint poslouchá na tailscale rozhraní, ne na `0.0.0.0`. S `host: tailscale`
si adresu zjistí sám přes `tailscale ip -4`; poslech na všech rozhraních jde
zapnout jen ručně přes `allow_public_bind: true`.

## Instalace — notebook (worker)

```bash
python3 -m venv ~/.local/share/voicenotes/venv
~/.local/share/voicenotes/venv/bin/pip install '/cesta/k/voicenotes[worker]'

mkdir -p ~/.config/voicenotes
cp config.example.yaml ~/.config/voicenotes/config.yaml
$EDITOR ~/.config/voicenotes/config.yaml     # remote.host, vault.root
```

Worker se na Pi hlásí přes SSH klíč bez hesla:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_voicenotes -N ''
ssh-copy-id -i ~/.ssh/id_ed25519_voicenotes voicenotes@<pi-tailscale>
```

Ruční běh a pak timer:

```bash
~/.local/share/voicenotes/venv/bin/voicenotes-worker -v \
  --config ~/.config/voicenotes/config.yaml

mkdir -p ~/.config/systemd/user
cp systemd/voicenotes-worker.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voicenotes-worker.timer
journalctl --user -u voicenotes-worker -f
```

Notebook běží nárazově — `Persistent=true` v timeru zajistí, že se po probuzení
zameškaný běh dožene.

## Syncthing

Pipeline **jen vytváří nové soubory, nikdy needituje existující** — jinak by
editace souboru, který zároveň mění telefon, vyrobila sync konflikt.

Rozepsané poznámky vznikají v `<vault>/.voicenotes-tmp/` a do inboxu se
přesouvají až hotové. Přidej do `.stignore` ve vaultu:

```
.voicenotes-tmp
```

Audio ve vaultu není a nebude; ve frontmatteru je jen cesta na Pi.

## Chybové stavy

| Situace | Chování |
|---|---|
| Špatný token | 401, do fronty se nic nezapíše |
| Soubor nad limit | 413, v `.tmp/` nezůstane půlka souboru |
| Nahrávka odeslaná dvakrát | Stejný obsah = stejný název, druhý pokus přepíše ten samý soubor |
| Nahrávka už zpracovaná | Gateway ji nevrátí do fronty, worker ji přeskočí podle evidence |
| Pi nedostupné | Worker skončí tiše, zkusí to příští běh |
| Přerušený přenos | Hash nesedí na název → poznámka nevznikne, originál zůstává na Pi |
| Cílová poznámka existuje | Přidá se suffix `-2`, existující soubor se nikdy nepřepíše |
| Zápis poznámky selže | Nahrávka zůstává ve frontě, další běh to zkusí znovu |
| Dva běhy najednou | Druhý zjistí zámek a skončí |

Nahrávka se z fronty na Pi jen přesouvá do `archive/`, nikdy nemaže. Lokální
kopie na notebooku se po zpracování uklidí.

## Konfigurace

Jeden `config.yaml` sdílený oběma stranami: sekce `gateway:` se čte na Pi,
`worker:` na notebooku. Komentovaný vzor je v `config.example.yaml`.
Tajemství v souboru nejsou — je tam jen jméno proměnné prostředí.

Pro vývoj na jednom stroji stačí `worker.remote.kind: local`, pak je „fronta na
Pi" jen složka na disku a rsync ani ssh se nepoužijí.

## Review poznámek

Týdenní review skript přijde ve fázi 4. Do té doby dotaz nad inboxem:

```dataview
TABLE created, duration_s, tags
FROM "Inbox"
WHERE status = "inbox" OR status = "needs-review"
SORT created ASC
```

Totéž jako Obsidian Base (`Inbox/_inbox.base`) — uprav podle své verze Obsidianu:

```yaml
filters:
  and:
    - 'status == "inbox"'
views:
  - type: table
    name: Ke zpracování
    order: [file.name, created, duration_s, tags]
```

## iOS Shortcut

Shortcut si stavíš sám, gateway sedí na tento kontrakt:

```
POST http://<pi-tailscale>:8080/ingest
Header: X-Auth-Token: <token z /etc/voicenotes/gateway.env>
Body:   multipart/form-data, pole "audio" = .m4a
→ 200 {"status": "queued", "id": "2026-08-14T143211-a3f9c1.m4a"}
→ 400 prázdný soubor nebo chybějící pole "audio"
→ 401 špatný token
→ 413 soubor nad limit (default 25 MB)
```

Ověření z terminálu:

```bash
curl -X POST http://<pi-tailscale>:8080/ingest \
  -H "X-Auth-Token: $VOICENOTES_TOKEN" \
  -F 'audio=@nahravka.m4a;type=audio/m4a'
```

## Vývoj

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,gateway]'
.venv/bin/pytest
```

Testy jsou vážené na chybové stavy: atomický zápis, idempotence hashe, kolize
názvů, špatný token, limit velikosti, nedostupné Pi, souběžné běhy. Happy path
je jeden, přes celou cestu od uploadu po soubor ve vaultu.

## Struktura

```
voicenotes/
├── config.py      načtení config.yaml, cesty, tajemství z prostředí
├── ids.py         název souboru = timestamp + hash obsahu (sdílí obě strany)
├── gateway.py     celý HTTP endpoint pro Pi
└── worker/
    ├── run.py     hlavní běh: stáhnout, zpracovat, uklidit
    ├── remote.py  fronta na Pi přes rsync/ssh (+ lokální varianta pro testy)
    ├── vault.py   zápis do vaultu — jen nové soubory, atomicky
    ├── notes.py   tvar markdown poznámky a frontmatteru
    ├── ledger.py  evidence zpracovaného (append-only JSONL, ne databáze)
    └── audio.py   délka nahrávky přes ffprobe
```

Gateway je jeden soubor podle zadání; sdílí s workerem jen `config.py` a
`ids.py`, aby se tvar názvu souboru nemohl na obou stranách rozejít.
