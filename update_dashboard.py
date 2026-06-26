#!/usr/bin/env python3
"""
update_dashboard.py
-------------------
Aktualisiert das "Alles auf Aktien"-WKN-Dashboard automatisch.

Ablauf:
  1. Folgen ueber die iTunes-Lookup-API holen
  2. Alle Folgen AB DEM STICHTAG (SINCE_DATE) auswaehlen
  3. WKNs + Wertpapiernamen aus den Folgenbeschreibungen parsen (ueber Folgen dedupliziert)
  4. Kennzahlen je WKN holen (1J/5J-Rendite, Dividendenrendite) ueber Yahoo Finance
  5. Pruefen, ob der Wert bei Trade Republic handelbar ist (Google-Such-API)
  6. index.html neu rendern

Aufruf:          python update_dashboard.py
Abhaengigkeiten: pip install requests

Benoetigte Umgebungsvariablen (als GitHub-Secrets) fuer die TR-Pruefung:
  GOOGLE_API_KEY  - API-Schluessel der Google Custom Search JSON API
  GOOGLE_CSE_ID   - ID einer Programmable Search Engine ("ganzes Web durchsuchen")
Beide kostenlos (100 Suchanfragen/Tag gratis). Fehlen sie, bleibt die Spalte leer ("-").
"""

import os
import re
import json
import html
import time
import datetime
import sys

import requests

PODCAST_ID = "1549709271"          # Alles auf Aktien (Apple Podcasts)
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
GOOGLE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
OUTPUT_HTML = "index.html"

# Stichtag: alle Folgen ab (einschliesslich) diesem Datum werden beruecksichtigt.
SINCE_DATE = datetime.date(2026, 6, 22)
EPISODE_FETCH_LIMIT = 200          # so viele Folgen von iTunes laden und nach Datum filtern (iTunes-Max ~200)
STATE_FILE = "seen_episodes.json"  # merkt sich bereits gesehene Folgen (Erkennung neuer Folgen)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
}

# ---------------------------------------------------------------------------
# WKN -> ISIN  (recherchiert; bei neuen Werten einfach ergaenzen)
# ---------------------------------------------------------------------------
WKN_ISIN = {
    "A0RPWH": "IE00B4L5Y983",   # iShares Core MSCI World
    "A2JAHJ": "NL0011683594",   # VanEck Dev. Markets Dividend Leaders
    "A2PKXG": "IE00BK5BQT80",   # Vanguard FTSE All-World (Acc)
    "LYX0Q0": "LU0908500753",   # Amundi Core STOXX Europe 600
    "A113FF": "IE00BM67HM91",   # Xtrackers MSCI World Energy
    "A1JKQL": "IE00B6R51Z18",   # iShares Oil & Gas Expl. & Production
    "A2QQ9R": "IE00BM8QRZ79",   # Invesco Solar Energy
    "A2AGZZ": "IE00BYTRR640",   # SPDR MSCI World Consumer Discretionary
    "A2PHCE": "IE00BJ5JP097",   # iShares MSCI World Financials
    "A2PHCL": "IE00BJ5JP659",   # iShares MSCI World Industrials
    "A1JX52": "IE00B3RBWM25",   # Vanguard FTSE All-World (Dist)
    "A0F5UH": "DE000A0F5UH1",   # iShares STOXX Global Select Dividend 100
    "A0S9GB": "DE000A0S9GB0",   # Xetra-Gold
    # noch zu ergaenzen (ISIN eintragen, dann liefern sie automatisch Daten):
    # "A1JJTD": "...",  "A1ELLY": "...",  "A0H08H": "...",
    # "EWG2LD": "...",  "EWG4CR": "...",  "A0N6XK": "...",  "A1KWPQ": "...",
}

YH = "https://query1.finance.yahoo.com"


# ---------------------------------------------------------------------------
# 1) Folgen holen
# ---------------------------------------------------------------------------
def get_episodes(limit=EPISODE_FETCH_LIMIT):
    params = {"id": PODCAST_ID, "entity": "podcastEpisode", "limit": limit}
    r = requests.get(ITUNES_LOOKUP, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return [x for x in r.json().get("results", []) if x.get("wrapperType") == "podcastEpisode"]


def episode_date(ep):
    """releaseDate (ISO) -> datetime.date oder None."""
    rel = (ep.get("releaseDate") or "")[:10]
    try:
        return datetime.date.fromisoformat(rel)
    except Exception:
        return None


def ep_id(ep):
    """Stabile ID je Folge (GUID bevorzugt, sonst trackId)."""
    return str(ep.get("episodeGuid") or ep.get("trackId") or ep.get("trackName", ""))


def episodes_in_range(episodes, since):
    """Alle Folgen ab Stichtag (bei unbekanntem Datum sicherheitshalber dabei)."""
    out = []
    for ep in episodes:
        d = episode_date(ep)
        if d is None or d >= since:
            out.append(ep)
    return out


def load_seen():
    """Menge bereits gesehener Folgen-IDs aus der Statusdatei."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def save_seen(ids):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ids": sorted(ids)}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  ! Statusdatei konnte nicht geschrieben werden: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 2) WKNs parsen
# ---------------------------------------------------------------------------
WKN_PATTERN = re.compile(r"([^()]{3,80}?)\s*\(WKN:\s*([A-Z0-9]{6})\)")

def extract_wkns(description_html):
    text = html.unescape(re.sub(r"<[^>]+>", " ", description_html or ""))
    found, seen = [], set()
    for m in WKN_PATTERN.finditer(text):
        wkn = m.group(2)
        if wkn not in seen:
            seen.add(wkn)
            found.append({"wkn": wkn, "name": m.group(1).strip(" ,-")})
    return found


# ---------------------------------------------------------------------------
# 3) Kennzahlen ueber Yahoo Finance
# ---------------------------------------------------------------------------
def yahoo_symbol(query):
    try:
        r = requests.get(f"{YH}/v1/finance/search",
                         params={"q": query, "quotesCount": 8, "newsCount": 0},
                         headers=HEADERS, timeout=20)
        quotes = r.json().get("quotes", [])
        if not quotes:
            return None
        pref = [q for q in quotes
                if str(q.get("symbol", "")).endswith(".DE")
                or q.get("exchange") in ("GER", "XETRA", "STU", "FRA")]
        return (pref or quotes)[0].get("symbol")
    except Exception:
        return None


def yahoo_chart(symbol):
    r = requests.get(f"{YH}/v8/finance/chart/{symbol}",
                     params={"range": "5y", "interval": "1d", "events": "div"},
                     headers=HEADERS, timeout=20)
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    close = res["indicators"]["quote"][0]["close"]
    divs = res.get("events", {}).get("dividends", {})
    return ts, adj, close, divs


def _last_valid(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _value_near(ts, values, target_ts):
    best, best_d = None, None
    for t, v in zip(ts, values):
        if v is None:
            continue
        d = abs(t - target_ts)
        if best_d is None or d < best_d:
            best, best_d = v, d
    return best if best_d is not None and best_d < 14 * 86400 else None


def fetch_metrics(wkn):
    result = {"y1": None, "y5": None, "div": None, "isin": WKN_ISIN.get(wkn)}
    try:
        symbol = yahoo_symbol(result["isin"]) if result["isin"] else None
        if not symbol:
            symbol = yahoo_symbol(wkn)
        if not symbol:
            return result
        ts, adj, close, divs = yahoo_chart(symbol)
        now = ts[-1]
        now_px = _last_valid(adj)
        if now_px:
            then1 = _value_near(ts, adj, now - 365 * 86400)
            then5 = _value_near(ts, adj, now - 5 * 365 * 86400)
            if then1:
                result["y1"] = round((now_px / then1 - 1) * 100, 1)
            if then5:
                result["y5"] = round((now_px / then5 - 1) * 100, 1)
        last_close = _last_valid(close)
        if last_close:
            cutoff = now - 365 * 86400
            paid = sum(d.get("amount", 0) for d in divs.values()
                       if d.get("date", 0) >= cutoff)
            result["div"] = round(paid / last_close * 100, 2)
        return result
    except Exception as e:
        print(f"  ! Kennzahlen fuer {wkn} fehlgeschlagen: {e}", file=sys.stderr)
        return result


# ---------------------------------------------------------------------------
# 4) Handelbarkeit bei Trade Republic (Google Custom Search JSON API)
# ---------------------------------------------------------------------------
def check_tradable(wkn, name):
    api_key = os.environ.get("GOOGLE_API_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        return None
    query = f"Ist WKN {wkn} {name} bei Trade Republic handelbar?"
    try:
        r = requests.get(GOOGLE_ENDPOINT,
                         params={"key": api_key, "cx": cse_id, "q": query,
                                 "num": 10, "hl": "de", "gl": "de"},
                         headers=HEADERS, timeout=20)
        items = r.json().get("items", [])
        if not items:
            return "unklar"
        blob = " ".join(
            f"{it.get('title','')} {it.get('snippet','')} {it.get('link','')}".lower()
            for it in items
        )
        on_tr_domain = "traderepublic.com" in blob
        positive = on_tr_domain or (
            "trade republic" in blob and any(k in blob for k in (
                "sparplanfaehig", "sparplanf\u00e4hig", "sparplan", "besparen",
                "handelbar", "kostenlos"))
        )
        negative = any(k in blob for k in (
            "nicht bei trade republic", "nicht handelbar bei trade republic",
            "bei trade republic nicht", "nicht im angebot von trade republic"))
        if negative and not on_tr_domain:
            return "Nein"
        if positive:
            return "Ja"
        return "unklar"
    except Exception as e:
        print(f"  ! TR-Check {wkn} fehlgeschlagen: {e}", file=sys.stderr)
        return "unklar"


# ---------------------------------------------------------------------------
# Aggregation: alle WKNs ab Stichtag einsammeln (ueber Folgen dedupliziert)
# ---------------------------------------------------------------------------
def collect_wkns_since(episodes, since):
    """Liefert Liste von Items {wkn,name,ep_title,ep_date(date)} ab Stichtag,
    nach Folgendatum absteigend, je WKN nur einmal (neueste Nennung)."""
    in_range = [(episode_date(ep), ep) for ep in episodes_in_range(episodes, since)]
    # neueste zuerst (None ans Ende)
    in_range.sort(key=lambda x: (x[0] is not None, x[0] or datetime.date.min), reverse=True)

    items, seen = [], set()
    for d, ep in in_range:
        title = ep.get("trackName", "")
        desc = ep.get("description") or ep.get("shortDescription") or ""
        for w in extract_wkns(desc):
            if w["wkn"] in seen:
                continue
            seen.add(w["wkn"])
            items.append({**w, "ep_title": title, "ep_date": d})
    return items


# ---------------------------------------------------------------------------
# 5) Rendern
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

def fmt_tr(v):
    if v is None:
        return '<span class="na">-</span>'
    if v == "Ja":
        return '<span class="tr-yes">&#10003; Ja</span>'
    if v == "Nein":
        return '<span class="tr-no">&#10007; Nein</span>'
    return '<span class="na">unklar</span>'

def fmt_ep(r):
    d = r.get("ep_date")
    dstr = d.strftime("%d.%m.%Y") if isinstance(d, datetime.date) else "-"
    title = html.escape((r.get("ep_title") or "")[:60])
    return f'<b>{dstr}</b><span class="epsub">{title}</span>'

def render_html(since, n_eps, rows):
    stamp = datetime.datetime.now().strftime("%d.%m.%Y, %H:%M")
    trs = "\n".join(
        "<tr>"
        f'<td class="wkn">{html.escape(r["wkn"])}</td>'
        f'<td>{html.escape(r["name"])}</td>'
        f'<td class="ep">{fmt_ep(r)}</td>'
        f'<td class="right">{fmt_pct(r["y1"])}</td>'
        f'<td class="right">{fmt_pct(r["y5"])}</td>'
        f'<td class="right">{fmt_div(r["div"])}</td>'
        f'<td class="center">{fmt_tr(r.get("tr"))}</td>'
        "</tr>"
        for r in rows
    ) or ('<tr><td colspan="7" class="na" style="text-align:center;padding:24px">'
          'Keine WKN in den Folgen ab dem Stichtag gefunden.</td></tr>')
    return TEMPLATE.format(since=since.strftime("%d.%m.%Y"), n_eps=n_eps,
                           n=len(rows), stamp=stamp, rows=trs)

TEMPLATE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alles auf Aktien - WKN-Dashboard</title>
<style>
  :root{{--bg:#0d1420;--card:#15223a;--line:#243650;--ink:#e8eef7;--muted:#8ea3bf;
         --dim:#5e7088;--gain:#46d39a;--loss:#f0697a;--gold:#e6b24c}}
  body{{margin:0;background:var(--bg);color:var(--ink);
        font-family:"Segoe UI",system-ui,Roboto,Arial,sans-serif;line-height:1.5}}
  .wrap{{max-width:1040px;margin:0 auto;padding:30px 18px 50px}}
  .eyebrow{{font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);font-weight:600}}
  h1{{font-size:clamp(24px,4vw,34px);margin:.3em 0 .2em}}
  .sub{{color:var(--muted);font-size:14.5px}}
  .stamp{{margin-top:8px;font-size:12.5px;color:var(--dim)}}
  .stamp b{{color:var(--muted)}}
  .scroll{{overflow-x:auto;margin-top:22px}}
  table{{border-collapse:collapse;width:100%;min-width:820px;
         background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}}
  th{{text-align:left;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
      color:var(--muted);padding:13px 14px;border-bottom:1px solid var(--line);background:#101a2b;white-space:nowrap}}
  th.right,td.right{{text-align:right}} th.center,td.center{{text-align:center}}
  td{{padding:12px 14px;border-bottom:1px solid rgba(36,54,80,.55);font-size:14px;
      font-variant-numeric:tabular-nums;vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  .wkn{{font-weight:700}}
  .ep{{font-size:12.5px;color:var(--muted);white-space:nowrap}}
  .epsub{{display:block;color:var(--dim);font-size:11px;white-space:normal;max-width:26ch}}
  .pos{{color:var(--gain);font-weight:600}} .neg{{color:var(--loss);font-weight:600}}
  .zero{{color:var(--muted)}} .has{{color:var(--gold);font-weight:600}} .na{{color:var(--dim)}}
  .tr-yes{{color:var(--gain);font-weight:600}} .tr-no{{color:var(--loss);font-weight:600}}
  .disc{{margin-top:18px;font-size:12px;color:var(--dim)}}
</style></head><body><div class="wrap">
  <div class="eyebrow">Alles auf Aktien - automatisch aktualisiert</div>
  <h1>WKN-Dashboard - Folgen seit {since}</h1>
  <p class="sub">{n_eps} Folge(n) ab {since} ausgewertet - {n} WKN(s) insgesamt</p>
  <div class="stamp">Letzte Aktualisierung: <b>{stamp}</b></div>
  <div class="scroll">
  <table>
    <thead><tr>
      <th>WKN</th><th>Wertpapier</th><th>Folge / Datum</th>
      <th class="right">Rendite 1&nbsp;J.</th><th class="right">Rendite 5&nbsp;J.</th>
      <th class="right">Div.-Rendite</th><th class="center">Handelbar auf Trade Republic</th>
    </tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
  </div>
  <p class="disc">Automatisch generiert aus allen Folgen ab {since}. Gesamtrendite inkl. Ausschuettungen
  (dividendenbereinigte Kurse); Dividendenrendite = Ausschuettungen der letzten 12 Monate / aktueller Kurs.
  Spalte "Handelbar auf Trade Republic": automatische Auswertung einer Google-Suche - Heuristik,
  "unklar" bei unsicherer Faktenlage; massgeblich ist die Suche in der Trade-Republic-App.
  Vergangene Wertentwicklung ist keine Prognose; keine Anlageempfehlung. "n. v." = nicht abrufbar.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
def main():
    print(f"Hole Folgen ... (Stichtag: {SINCE_DATE.strftime('%d.%m.%Y')})")
    episodes = get_episodes()
    if not episodes:
        print("Keine Folgen gefunden - Abbruch.", file=sys.stderr)
        sys.exit(1)

    in_range = episodes_in_range(episodes, SINCE_DATE)
    n_eps = len(in_range)

    # Neue Folgen gegenueber dem letzten Lauf erkennen
    seen_ids = load_seen()
    current_ids = {ep_id(ep) for ep in in_range}
    new_eps = [ep for ep in in_range if ep_id(ep) not in seen_ids]
    if new_eps:
        print(f"Neue Folge(n) seit letztem Lauf: {len(new_eps)}")
        for ep in new_eps:
            d = episode_date(ep)
            ds = d.strftime("%d.%m.%Y") if d else "?"
            print(f"   + [{ds}] {ep.get('trackName','')}")
    else:
        print("Keine neuen Folgen seit letztem Lauf.")

    # Warnung, falls das Zeitfenster die Abruf-Obergrenze erreicht haben koennte
    dates = [d for d in (episode_date(ep) for ep in episodes) if d]
    if len(episodes) >= EPISODE_FETCH_LIMIT and dates and min(dates) >= SINCE_DATE:
        print("  ! Achtung: aelteste abgerufene Folge liegt noch im Zeitfenster - "
              "EPISODE_FETCH_LIMIT ggf. erhoehen oder SINCE_DATE anpassen.", file=sys.stderr)

    items = collect_wkns_since(episodes, SINCE_DATE)
    print(f"{n_eps} Folge(n) ab Stichtag, {len(items)} WKN(s) gesamt.")

    rows = []
    for it in items:
        m = fetch_metrics(it["wkn"])
        tr = check_tradable(it["wkn"], it["name"])
        dstr = it["ep_date"].strftime("%d.%m.%Y") if it["ep_date"] else "?"
        print(f"  -> {it['wkn']:7} [{dstr}] 1J={m['y1']} 5J={m['y5']} Div={m['div']} TR={tr}")
        rows.append({**it, **m, "tr": tr})
        time.sleep(0.6)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(SINCE_DATE, n_eps, rows))

    # Gesehene Folgen fortschreiben (fuer die Erkennung beim naechsten Lauf)
    save_seen(seen_ids | current_ids)
    print(f"Fertig: {OUTPUT_HTML} ({len(rows)} Zeilen) geschrieben.")


if __name__ == "__main__":
    main()
