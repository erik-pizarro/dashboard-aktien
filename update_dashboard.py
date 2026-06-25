#!/usr/bin/env python3
"""
update_dashboard.py
-------------------
Aktualisiert das "Alles auf Aktien"-WKN-Dashboard automatisch.

Ablauf:
  1. Neueste Folge(n) über die iTunes-Lookup-API holen
  2. WKNs + Wertpapiernamen aus der Folgenbeschreibung parsen
  3. Kennzahlen je WKN holen (1J/5J-Rendite, Dividendenrendite)  <-- austauschbar
  4. dashboard.html neu rendern

Aufruf:        python update_dashboard.py
Abhaengigkeiten: pip install requests beautifulsoup4

WICHTIG: Schritt 3 (fetch_metrics) ist der fragile Teil. Die Beispiel-Implementierung
ist "best effort" und gibt bei Problemen None zurueck (-> "n. v." im Dashboard),
damit der Agent nie abstuerzt. Fuer den Dauerbetrieb durch eine lizenzierte
Marktdaten-API ersetzen (siehe TODO unten).
"""

import re
import json
import html
import datetime
import sys

import requests

PODCAST_ID = "1549709271"        # Alles auf Aktien (Apple Podcasts)
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
HEADERS = {"User-Agent": "aaa-dashboard-bot/1.0 (privat, nicht-kommerziell)"}
OUTPUT_HTML = "dashboard.html"
EPISODES_TO_SCAN = 1             # 1 = nur die aktuellste Folge; hoeher = mehrere Folgen sammeln


# ---------------------------------------------------------------------------
# 1) Folgen holen
# ---------------------------------------------------------------------------
def get_episodes(limit=10):
    params = {"id": PODCAST_ID, "entity": "podcastEpisode", "limit": limit}
    r = requests.get(ITUNES_LOOKUP, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    results = r.json().get("results", [])
    # results[0] ist der Podcast selbst, danach die Episoden (neueste zuerst)
    episodes = [x for x in results if x.get("wrapperType") == "podcastEpisode"]
    return episodes


# ---------------------------------------------------------------------------
# 2) WKNs parsen
# ---------------------------------------------------------------------------
# Faengt "Name (WKN: A0RPWH)" ein. WKN = 6 alphanumerische Zeichen.
WKN_PATTERN = re.compile(r"([A-Za-zÄÖÜäöü0-9&.\-/ ]{3,80}?)\s*\(WKN:\s*([A-Z0-9]{6})\)")

def extract_wkns(description_html):
    text = re.sub(r"<[^>]+>", " ", description_html or "")   # HTML-Tags entfernen
    text = html.unescape(text)
    found = []
    seen = set()
    for m in WKN_PATTERN.finditer(text):
        name = m.group(1).strip(" ,–-")
        wkn = m.group(2)
        if wkn not in seen:
            seen.add(wkn)
            found.append({"wkn": wkn, "name": name})
    return found


# ---------------------------------------------------------------------------
# 3) Kennzahlen holen  ---  FRAGILER TEIL: bitte pruefen / ersetzen
# ---------------------------------------------------------------------------
def fetch_metrics(wkn):
    """
    Gibt {'y1':float|None, 'y5':float|None, 'div':float|None, 'isin':str|None} zurueck.
    Bei Fehlern -> alles None (Dashboard zeigt dann "n. v.").

    TODO (Produktion): durch eine lizenzierte API ersetzen, z. B.
        - Twelve Data            (twelvedata.com)        -> /etf, /time_series
        - Financial Modeling Prep(financialmodelingprep.com)
        - EOD Historical Data    (eodhd.com)
    Diese liefern ISIN/Ticker-basiert verlaessliche Performance- und
    Dividendendaten ohne bruechiges HTML-Scraping.
    """
    try:
        # Platzhalter-Implementierung: hier deinen API-Call einsetzen.
        # Beispiel-Skelett fuer eine API mit Key:
        #
        #   api_key = os.environ["MARKETDATA_API_KEY"]
        #   resp = requests.get(f"https://api.example.com/etf/{wkn}",
        #                       params={"apikey": api_key}, timeout=20)
        #   d = resp.json()
        #   return {"y1": d["return_1y"], "y5": d["return_5y"],
        #           "div": d["dividend_yield"], "isin": d["isin"]}
        #
        return {"y1": None, "y5": None, "div": None, "isin": None}
    except Exception as e:                       # nie crashen lassen
        print(f"  ! Kennzahlen fuer {wkn} fehlgeschlagen: {e}", file=sys.stderr)
        return {"y1": None, "y5": None, "div": None, "isin": None}


# ---------------------------------------------------------------------------
# 4) Rendern
# ---------------------------------------------------------------------------
def fmt_pct(v):
    if v is None:
        return '<span class="na">n. v.</span>'
    cls = "pos" if v > 0 else ("neg" if v < 0 else "zero")
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}{str(v).replace(".", ",")}&nbsp;%</span>'

def fmt_div(v):
    if v is None:
        return '<span class="na">n. v.</span>'
    if v == 0:
        return '<span class="zero">0&nbsp;%</span>'
    return f'<span class="has">{str(v).replace(".", ",")}&nbsp;%</span>'

def render_html(episode_title, episode_date, rows):
    stamp = datetime.datetime.now().strftime("%d.%m.%Y, %H:%M")
    trs = []
    for r in rows:
        trs.append(
            "<tr>"
            f'<td class="wkn">{html.escape(r["wkn"])}</td>'
            f'<td>{html.escape(r["name"])}</td>'
            f'<td class="right">{fmt_pct(r["y1"])}</td>'
            f'<td class="right">{fmt_pct(r["y5"])}</td>'
            f'<td class="right">{fmt_div(r["div"])}</td>'
            "</tr>"
        )
    rows_html = "\n".join(trs) if trs else (
        '<tr><td colspan="5" class="na" style="text-align:center;padding:24px">'
        'In der aktuellen Folge wurde keine WKN gefunden.</td></tr>'
    )
    return TEMPLATE.format(
        title=html.escape(episode_title),
        ep_date=html.escape(episode_date),
        stamp=stamp,
        n=len(rows),
        rows=rows_html,
    )

TEMPLATE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alles auf Aktien – WKN-Dashboard</title>
<style>
  :root{{--bg:#0d1420;--card:#15223a;--line:#243650;--ink:#e8eef7;--muted:#8ea3bf;
         --dim:#5e7088;--gain:#46d39a;--loss:#f0697a;--gold:#e6b24c}}
  body{{margin:0;background:var(--bg);color:var(--ink);
        font-family:"Segoe UI",system-ui,Roboto,Arial,sans-serif;line-height:1.5}}
  .wrap{{max-width:920px;margin:0 auto;padding:30px 18px 50px}}
  .eyebrow{{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);font-weight:600}}
  h1{{font-size:clamp(24px,4vw,34px);margin:.3em 0 .2em}}
  .sub{{color:var(--muted);font-size:14.5px}}
  .stamp{{margin-top:8px;font-size:12.5px;color:var(--dim)}}
  .stamp b{{color:var(--muted)}}
  table{{border-collapse:collapse;width:100%;margin-top:22px;
         background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}}
  th{{text-align:left;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
      color:var(--muted);padding:13px 14px;border-bottom:1px solid var(--line);background:#101a2b}}
  th.right,td.right{{text-align:right}}
  td{{padding:12px 14px;border-bottom:1px solid rgba(36,54,80,.55);font-size:14px;
      font-variant-numeric:tabular-nums}}
  tr:last-child td{{border-bottom:none}}
  .wkn{{font-weight:700}}
  .pos{{color:var(--gain);font-weight:600}} .neg{{color:var(--loss);font-weight:600}}
  .zero{{color:var(--muted)}} .has{{color:var(--gold);font-weight:600}} .na{{color:var(--dim)}}
  .disc{{margin-top:18px;font-size:12px;color:var(--dim)}}
</style></head><body><div class="wrap">
  <div class="eyebrow">Alles auf Aktien · automatisch aktualisiert</div>
  <h1>WKN-Dashboard – aktuelle Folge</h1>
  <p class="sub">Folge: „{title}“ ({ep_date}) · {n} WKN(s) gefunden</p>
  <div class="stamp">Letzte Aktualisierung: <b>{stamp}</b></div>
  <table>
    <thead><tr>
      <th>WKN</th><th>Wertpapier</th>
      <th class="right">Rendite 1&nbsp;J.</th><th class="right">Rendite 5&nbsp;J.</th>
      <th class="right">Div.-Rendite</th>
    </tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
  <p class="disc">Automatisch generiert. Vergangene Wertentwicklung ist keine Prognose; keine Anlageempfehlung.
  „n. v.“ = Kennzahl konnte nicht abgerufen werden.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("Hole Folgen ...")
    episodes = get_episodes()
    if not episodes:
        print("Keine Folgen gefunden – Abbruch.", file=sys.stderr)
        sys.exit(1)

    rows = []
    title = date = ""
    for ep in episodes[:EPISODES_TO_SCAN]:
        title = ep.get("trackName", "")
        rel = ep.get("releaseDate", "")[:10]
        try:
            date = datetime.date.fromisoformat(rel).strftime("%d.%m.%Y")
        except Exception:
            date = rel
        desc = ep.get("description") or ep.get("shortDescription") or ""
        wkns = extract_wkns(desc)
        print(f"Folge: {title} ({date}) – {len(wkns)} WKN(s)")
        for item in wkns:
            print(f"  -> {item['wkn']}  {item['name']}")
            m = fetch_metrics(item["wkn"])
            rows.append({**item, **m})

    out = render_html(title, date, rows)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Fertig: {OUTPUT_HTML} ({len(rows)} Zeilen) geschrieben.")


if __name__ == "__main__":
    main()
