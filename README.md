# thousandeyes-rum-splunk

Coletor das sessões de browser de usuários reais do **Cisco ThousandEyes** (Endpoint Experience, *Real User Tests*)
para o **Splunk** via HTTP Event Collector (HEC).

A integração do ThousandEyes com o Splunk por OpenTelemetry envia métricas de testes, mas não envia a navegação real
dos usuários com Endpoint Agent. Este coletor consulta a API v7 do ThousandEyes a cada 5 minutos e grava no Splunk um
evento por página visitada, com os dados da sessão:

| Campo | Descrição |
|---|---|
| `servico` | nome do serviço, pelo `SERVICE_MAP` (senão o host da URL) |
| `agentName`, `computerName` | máquina com o Endpoint Agent |
| `sourceAddress` | IP da estação (chave para correlacionar com firewall, flow logs e APM) |
| `visitedSite`, `pageUrl`, `pageTitle` | site e página visitados |
| `responseCode`, `responseTime`, `onLoad`, `onContentLoad` | resultado e tempos da página |
| `experienceScore` | score de experiência da sessão (0 a 1) |
| `sessionId`, `roundId`, `tipo` | identificação; `tipo=sessao` quando a página ainda não tem detalhe |

Sourcetype: `cisco:thousandeyes:rum`. Endpoints usados: `POST /v7/endpoint/test-results/real-user-tests/filter`,
`POST /v7/endpoint/test-results/real-user-tests/pages/filter` e `GET /v7/endpoint/agents`.

## Requisitos

- Linux com Python 3.8+ (só biblioteca padrão) e systemd.
- Saída HTTPS para `api.thousandeyes.com` e para o HEC do Splunk (proxy via `HTTPS_PROXY`, se necessário).
- Token OAuth Bearer da API do ThousandEyes de um usuário com leitura de Endpoint
  (ThousandEyes → *My Profile* → *User API Tokens*).
- Perfil de *Real User Tests* ativo no ThousandEyes para os domínios desejados.

## Preparação no Splunk

1. Índice de destino (ex.: `thousandeyes`).
2. Token HEC (Settings → Data inputs → HTTP Event Collector) com esse índice permitido e sourcetype
   `cisco:thousandeyes:rum`.
3. Extração JSON do sourcetype (`props.conf` em um app, ou Settings → Source types):

```
[cisco:thousandeyes:rum]
KV_MODE = json
SHOULD_LINEMERGE = false
```

## Instalação (como root)

```bash
git clone https://github.com/gregoriooliveira/thousandeyes-rum-splunk.git
cd thousandeyes-rum-splunk
useradd --system --no-create-home --shell /usr/sbin/nologin te_rum
install -d -o te_rum -g te_rum /opt/te_rum_splunk /var/lib/te_rum_splunk
install -o te_rum -g te_rum -m 0755 te_rum_splunk.py /opt/te_rum_splunk/
install -o root -g te_rum -m 0640 te_rum_splunk.env.exemplo /etc/te_rum_splunk.env
vi /etc/te_rum_splunk.env            # preencher tokens, account group, domínios e HEC
install -m 0644 te_rum_splunk.service te_rum_splunk.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start te_rum_splunk.service && journalctl -u te_rum_splunk -n 5   # teste manual
systemctl enable --now te_rum_splunk.timer
```

Cada execução registra no journal a janela consultada, as sessões, as páginas e os eventos enviados. Na primeira
execução são lidos os últimos 15 minutos; depois de uma parada, recupera até 6 horas (estado em `STATE_FILE`).

## Configuração (`/etc/te_rum_splunk.env`)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TE_TOKEN` | sim | token OAuth Bearer da API do ThousandEyes |
| `TE_AID` | sim | ID do account group |
| `TE_DOMAINS` | sim | domínios monitorados, separados por vírgula |
| `SERVICE_MAP` | não | `trecho_da_url=Nome` separados por vírgula (ex.: `erp.=ERP,/portal=Portal`) |
| `HEC_URL` | sim | URL base do HEC (ex.: `https://http-inputs-<stack>.splunkcloud.com:443`) |
| `HEC_TOKEN` | sim | token HEC |
| `HEC_INDEX` | não | índice (padrão `thousandeyes`) |
| `STATE_FILE` | não | arquivo de estado (padrão `/var/lib/te_rum_splunk/estado.json`) |

## Validação no Splunk

```
index=thousandeyes sourcetype=cisco:thousandeyes:rum earliest=-30m
| stats count dc(sourceAddress) as estacoes avg(experienceScore) as score
        count(eval(responseCode>=400 OR responseCode=0)) as erros by servico
```

## Operação

- Parar: `systemctl disable --now te_rum_splunk.timer`.
- Trocar token: editar `/etc/te_rum_splunk.env` (cada execução relê o arquivo).
- Limite de API (HTTP 429): o script aguarda e repete.
- Os tokens ficam só no arquivo de ambiente; nada é gravado em log.

## Licença

MIT.
