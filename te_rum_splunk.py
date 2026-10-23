#!/usr/bin/env python3
"""Coletor das sessões de browser de usuário real (ThousandEyes Endpoint, Real User Tests) para o Splunk via HEC.

A integração OpenTelemetry do ThousandEyes não envia esses dados; este script consulta a API v7:
  POST /v7/endpoint/test-results/real-user-tests/filter        (sessões: IP da estação, site, experience score)
  POST /v7/endpoint/test-results/real-user-tests/pages/filter  (páginas: URL, código HTTP, tempos de carga)
  GET  /v7/endpoint/agents                                      (nome da máquina e usuário do agente)
e grava um evento por página visitada (com os dados da sessão) no Splunk, sourcetype cisco:thousandeyes:rum.

Configuração por variáveis de ambiente (arquivo /etc/te_rum_splunk.env no systemd):
  TE_TOKEN     token OAuth Bearer da API do ThousandEyes (usuário com leitura de Endpoint)
  TE_AID       ID do account group do ThousandEyes
  TE_DOMAINS   domínios monitorados no perfil de Real User Tests, separados por vírgula (ex.: empresa.com.br)
  SERVICE_MAP  opcional: trecho_da_url=Nome, separados por vírgula (ex.: erp.=ERP,/portal=Portal);
               o campo servico recebe o primeiro nome cujo trecho aparece na URL, senão o host da URL
  HEC_URL      URL base do HEC (ex.: https://http-inputs-<stack>.splunkcloud.com:443)
  HEC_TOKEN    token HEC
  HEC_INDEX    índice (padrão: thousandeyes)
  STATE_FILE   arquivo de estado (padrão: /var/lib/te_rum_splunk/estado.json)
Executar a cada 5 min (systemd timer ou cron). Cada execução busca do último fim processado até agora-2min.
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.thousandeyes.com/v7"
TE_TOKEN = os.environ.get("TE_TOKEN", "")
TE_AID = os.environ.get("TE_AID", "")
DOMAINS = [d.strip() for d in os.environ.get("TE_DOMAINS", "").split(",") if d.strip()]
SERVICE_MAP = [tuple(p.split("=", 1)) for p in os.environ.get("SERVICE_MAP", "").split(",") if "=" in p]
HEC_URL = os.environ.get("HEC_URL", "").rstrip("/") + "/services/collector/event"
HEC_TOKEN = os.environ.get("HEC_TOKEN", "")
HEC_INDEX = os.environ.get("HEC_INDEX", "thousandeyes")
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/te_rum_splunk/estado.json")
MAX_JANELA = timedelta(hours=6)  # na primeira execução ou após parada longa, recupera no máximo 6 h


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def te(method, path, params=None, body=None):
    """Chamada à API do TE com paginação por _links.next e espera em 429."""
    url = f"{API}{path}?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v})
    out = []
    while url:
        for tentativa in range(5):
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Authorization": f"Bearer {TE_TOKEN}", "Accept": "application/hal+json",
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read() or b"{}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and tentativa < 4:
                    espera = int(e.headers.get("x-organization-rate-limit-reset", "0") or 0) - int(time.time())
                    time.sleep(min(max(espera, 5), 60))
                    continue
                raise RuntimeError(f"TE {method} {path}: HTTP {e.code} {e.read()[:300]!r}")
        out.extend(data.get("results") or data.get("agents") or [])
        nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
        url = nxt if nxt and nxt != url else None
    return out


def epoch(quando):
    """Data ISO 8601 da API (ex.: 2026-10-23T13:14:06.65+00:00 ou ...Z) em epoch; aceita Python < 3.11."""
    if not quando:
        return time.time()
    s = quando.strip().replace("Z", "+00:00")
    base, fuso = s[:19], s[19:]
    frac = 0.0
    if fuso.startswith("."):
        i = 1
        while i < len(fuso) and fuso[i].isdigit():
            i += 1
        frac, fuso = float("0" + fuso[:i]), fuso[i:]
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    if fuso:
        sinal = -1 if fuso[0] == "-" else 1
        hh, mm = fuso[1:].replace(":", "")[:2], fuso[1:].replace(":", "")[2:4] or "0"
        dt = dt.replace(tzinfo=timezone(sinal * timedelta(hours=int(hh), minutes=int(mm))))
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() + frac


def servico(url):
    u = (url or "").lower()
    for trecho, nome in SERVICE_MAP:
        if trecho.strip().lower() in u:
            return nome.strip()
    return urllib.parse.urlparse(u if "//" in u else "//" + u).hostname or "outro"

def hec(eventos):
    for i in range(0, len(eventos), 200):
        lote = "".join(json.dumps(e, ensure_ascii=False) for e in eventos[i:i + 200]).encode()
        req = urllib.request.Request(HEC_URL, data=lote, method="POST",
                                     headers={"Authorization": f"Splunk {HEC_TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError(f"HEC HTTP {r.status}")


def main():
    for n, v in (("TE_TOKEN", TE_TOKEN), ("TE_AID", TE_AID), ("TE_DOMAINS", ",".join(DOMAINS)), ("HEC_URL", HEC_URL), ("HEC_TOKEN", HEC_TOKEN)):
        if not v or v == "/services/collector/event":
            sys.exit(f"variável {n} não definida")
    agora = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    try:
        inicio = datetime.fromtimestamp(epoch(json.load(open(STATE_FILE))["fim"]), timezone.utc)
    except (OSError, ValueError, KeyError):
        inicio = agora - timedelta(minutes=15)
    inicio = max(inicio, agora - MAX_JANELA)
    if inicio >= agora:
        return
    params = {"aid": TE_AID, "startDate": inicio.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDate": agora.strftime("%Y-%m-%dT%H:%M:%SZ")}
    filtro = {"searchFilters": {"domain": DOMAINS}}

    agentes = {a.get("id") or a.get("agentId"): a for a in te("GET", "/endpoint/agents", {"aid": TE_AID})}
    sessoes = {s.get("id"): s for s in te("POST", "/endpoint/test-results/real-user-tests/filter", params, filtro)}
    paginas = te("POST", "/endpoint/test-results/real-user-tests/pages/filter", params, filtro)

    eventos, com_pagina = [], set()
    for p in paginas:
        s = sessoes.get(p.get("id"), {})
        com_pagina.add(p.get("id"))
        a = agentes.get(p.get("agentId") or s.get("agentId"), {})
        t = p.get("pageTimings") or {}
        ev = {"tipo": "pagina", "servico": servico(p.get("pageUrl") or s.get("visitedSite")),
              "agentId": p.get("agentId") or s.get("agentId"), "agentName": a.get("agentName") or a.get("name"),
              "computerName": a.get("computerName"),
              "sourceAddress": s.get("sourceAddress"), "visitedSite": s.get("visitedSite"), "experienceScore": s.get("experienceScore"),
              "sessionId": p.get("id"), "roundId": p.get("roundId"), "pageId": p.get("pageId"), "pageTitle": p.get("pageTitle"),
              "pageUrl": p.get("pageUrl"), "responseCode": p.get("responseCode"), "responseTime": p.get("responseTime"),
              "onContentLoad": t.get("onContentLoad"), "onLoad": t.get("onLoad"), "loadDate": p.get("loadDate"),
              "cpuMax": ((p.get("systemMetrics") or {}).get("cpuUtilization") or {}).get("max")}
        eventos.append({"time": epoch(p.get("loadDate") or s.get("date")),
                        "index": HEC_INDEX, "sourcetype": "cisco:thousandeyes:rum", "source": "te_rum_splunk",
                        "host": a.get("agentName") or a.get("name") or "thousandeyes", "event": {k: v for k, v in ev.items() if v is not None}})
    for sid, s in sessoes.items():  # sessão sem página detalhada ainda: grava a sessão
        if sid in com_pagina:
            continue
        a = agentes.get(s.get("agentId"), {})
        ev = {"tipo": "sessao", "servico": servico(s.get("visitedSite")), "agentId": s.get("agentId"), "agentName": a.get("agentName") or a.get("name"),
              "computerName": a.get("computerName"), "sourceAddress": s.get("sourceAddress"), "visitedSite": s.get("visitedSite"),
              "experienceScore": s.get("experienceScore"), "numberOfPages": s.get("numberOfPages"), "sessionId": sid, "roundId": s.get("roundId")}
        eventos.append({"time": epoch(s.get("date")),
                        "index": HEC_INDEX, "sourcetype": "cisco:thousandeyes:rum", "source": "te_rum_splunk",
                        "host": a.get("agentName") or a.get("name") or "thousandeyes", "event": {k: v for k, v in ev.items() if v is not None}})
    if eventos:
        hec(eventos)
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    json.dump({"fim": agora.isoformat()}, open(STATE_FILE, "w"))
    log(f"{params['startDate']} a {params['endDate']}: {len(sessoes)} sessões, {len(paginas)} páginas, {len(eventos)} eventos enviados")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - registrar e sair com erro para o systemd
        log(f"ERRO {e}")
        sys.exit(1)
