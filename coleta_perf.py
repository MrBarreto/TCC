#!/usr/bin/env python3
"""Coleta perf stat no host da VM durante uma rodada de clientes.

Roda no host da VM, como root:   sudo ./coleta_perf.py [--cpus 0-5] [--duracao 60] [--periodo 120]

Escuta na porta 8000 e entende duas mensagens, sem parametros (enviadas pelo clientes.py):
  POST /siga   comeca a coletar: uma janela de --duracao s a cada --periodo s,
               a primeira imediatamente (as seguintes em 0, periodo, 2*periodo, ...)
               Com ?rodada=n64_r1 a pasta se chama <outdir>/n64_r1/ (se ja existir, e reaproveitada);
               sem o parametro, <outdir>/sessao_<epoch>/
  POST /pare   encerra: interrompe a janela em curso, se houver, e cancela as proximas
  GET  /status mostra se ha coleta em curso

Cada /siga cria uma pasta de sessao (nome acima) com:
  metricas.csv  UMA LINHA POR JANELA: epoch de inicio e fim, contagens totais das CPUs
                (somadas), IPC e faltas por mil instrucoes (MPKI)
  eventos.log   mensagens recebidas e erros do perf
  wNN.csv       saida bruta do perf stat; so e mantida se a janela falhar ou nao tiver nenhuma
                contagem (para diagnostico). As janelas normais ficam so no metricas.csv.
Eventos (Secao 3.3): cycles, instructions, L1-dcache-load-misses, L1-icache-load-misses,
LLC-load-misses, lidos em grupo. Contam toda a atividade das CPUs (guest e host).
Colunas de metricas.csv: ipc = instructions/cycles; *_mpki = 1000*misses/instructions;
cpus_uso_pct = uso das CPUs de --cpus (somadas) e global_uso_pct = uso de todas as CPUs do host (%),
calculados com as diferencas de /proc/stat entre o inicio e o fim do perf (media da janela):
100 * (1 - (idle+iowait) / total); inclui o tempo do convidado;
cobertura_min_pct = menor % do tempo em que um evento foi contado (100 = sem multiplexacao);
sem_contagem = eventos que o perf nao conseguiu contar (nao suportado / nao contado).
"""
import argparse
import csv
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# O grupo {} faz o perf ler os cinco eventos juntos. Atencao: se um evento do grupo nao for
# suportado pela CPU, o grupo inteiro vira <not counted>.
EVENTOS = "{cycles,instructions,L1-dcache-load-misses,L1-icache-load-misses,LLC-load-misses}"
CAMPOS = ["janela", "epoch_inicio", "epoch_fim", "duracao_s", "interrompida", "codigo_saida",
          "cycles", "instructions", "l1d_misses", "l1i_misses", "llc_misses",
          "ipc", "l1d_mpki", "l1i_mpki", "llc_mpki", "cpus_uso_pct", "global_uso_pct",
          "cobertura_min_pct", "sem_contagem"]


def agora():
    return time.strftime("%H:%M:%S")


def le_contagens(caminho):
    """{evento: (valor ou None, % do tempo contado ou None)} da saida -x, do perf stat."""
    r = {}
    try:
        linhas = open(caminho).read().splitlines()
    except OSError:
        return r
    for l in linhas:
        c = l.split(",")
        if not l or l.startswith("#") or len(c) < 5:
            continue
        try:
            valor = float(c[0])
        except ValueError:          # <not counted>, <not supported>
            valor = None
        try:
            pct = float(c[4])
        except ValueError:
            pct = None
        r[c[2]] = (valor, pct)
    return r


def lista_cpus(spec):
    """'0-5' ou '0,2-4' -> [0, 1, 2, 3, 4, 5] ou [0, 2, 3, 4]."""
    r = []
    for p in spec.split(","):
        if "-" in p:
            a, b = p.split("-")
            r += range(int(a), int(b) + 1)
        else:
            r.append(int(p))
    return r


def le_proc_stat(cpus):
    """{'cpus': (ocioso, total), 'global': (ocioso, total)} em jiffies, de /proc/stat: soma das
    CPUs de `cpus` e linha 'cpu' (todas). total = user..steal; guest ja esta dentro de user."""
    r = {"cpus": [0, 0], "global": [0, 0]}
    with open("/proc/stat") as f:
        for l in f:
            c = l.split()
            if not c or not c[0].startswith("cpu"):
                continue
            v = ([int(x) for x in c[1:9]] + [0] * 8)[:8]  # user nice system idle iowait irq softirq steal
            par = (v[3] + v[4], sum(v))
            if c[0] == "cpu":
                r["global"] = list(par)
            elif int(c[0][3:]) in cpus:
                r["cpus"][0] += par[0]
                r["cpus"][1] += par[1]
    return r


def uso_cpu(antes, depois):
    """{'cpus_uso_pct', 'global_uso_pct'}: 100*(1 - d_ocioso/d_total) entre duas leituras."""
    r = {}
    for esc in ("cpus", "global"):
        d_oc = depois[esc][0] - antes[esc][0]
        d_tot = depois[esc][1] - antes[esc][1]
        r[f"{esc}_uso_pct"] = round(100.0 * (1 - d_oc / d_tot), 2) if d_tot > 0 else None
    return r


def linha_metricas(k, t_ini, t_fim, interrompida, rc, cont, uso):
    g = lambda e: cont.get(e, (None, None))[0]
    cyc, ins = g("cycles"), g("instructions")
    dm, im, lm = g("L1-dcache-load-misses"), g("L1-icache-load-misses"), g("LLC-load-misses")
    mpki = lambda m: round(1000 * m / ins, 4) if m is not None and ins else None
    pcts = [p for _, p in cont.values() if p is not None]
    r = {
        "janela": k, "epoch_inicio": f"{t_ini:.3f}", "epoch_fim": f"{t_fim:.3f}",
        "duracao_s": f"{t_fim - t_ini:.1f}", "interrompida": interrompida, "codigo_saida": rc,
        "cycles": cyc, "instructions": ins, "l1d_misses": dm, "l1i_misses": im, "llc_misses": lm,
        "ipc": round(ins / cyc, 4) if cyc and ins is not None else None,
        "l1d_mpki": mpki(dm), "l1i_mpki": mpki(im), "llc_mpki": mpki(lm),
        **uso,
        "cobertura_min_pct": min(pcts) if pcts else None,
        "sem_contagem": ";".join(e for e, (v, _) in cont.items() if v is None),
    }
    return {c: ("" if v is None else (int(v) if isinstance(v, float) and v == int(v) else v))
            for c, v in r.items()}


class Coletor:
    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.parar = threading.Event()
        self.thread = None
        self.proc = None

    def ativo(self):
        return self.thread is not None and self.thread.is_alive()

    def siga(self, rodada=None):
        """Inicia a coleta. Retorna (codigo HTTP, texto): 200 com o nome da pasta da sessao, ou 409
        se ja ha coleta em curso. Se a pasta de `rodada` ja existe, os arquivos sao sobrescritos."""
        with self.lock:
            if self.ativo():
                return 409, "ja ha coleta em curso"
            if rodada:
                nome = rodada
            else:
                base = f"sessao_{time.time():.0f}"
                nome, i = base, 1
                while os.path.exists(os.path.join(self.a.outdir, nome)):
                    i += 1
                    nome = f"{base}_{i}"
            pasta = os.path.join(self.a.outdir, nome)
            os.makedirs(pasta, exist_ok=True)
            self.parar.clear()
            self.thread = threading.Thread(target=self.laco, args=(pasta,), daemon=True)
            self.thread.start()
            return 200, nome

    def pare(self):
        """Encerra a coleta. Retorna True se havia coleta em curso."""
        with self.lock:
            estava = self.ativo()
            self.parar.set()
            if self.proc is not None and self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGINT)  # perf fecha a contagem e sai
                except OSError:
                    pass
            t = self.thread
        if t is not None:
            t.join(timeout=15)
        return estava

    def laco(self, pasta):
        a = self.a
        with open(os.path.join(pasta, "eventos.log"), "w", buffering=1) as ev, \
                open(os.path.join(pasta, "metricas.csv"), "w", newline="", buffering=1) as mf:
            w = csv.DictWriter(mf, fieldnames=CAMPOS)
            w.writeheader()
            ev.write(f"{time.time():.3f} siga\n")
            m0 = time.monotonic()
            k = 0
            while True:
                k += 1
                arq = os.path.join(pasta, f"w{k:02d}.csv")
                cmd = ["perf", "stat", "-a", "-C", a.cpus, "-x", ",", "-e", a.eventos,
                       "-o", arq, "--", "sleep", str(a.duracao)]
                with self.lock:           # evita iniciar um perf depois de um /pare
                    if self.parar.is_set():
                        break
                    t_ini = time.time()
                    antes = le_proc_stat(a.cpu_lista)
                    try:
                        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.DEVNULL, stderr=ev,
                                                     start_new_session=True)
                    except OSError as e:
                        ev.write(f"{time.time():.3f} erro ao iniciar o perf: {e}\n")
                        break
                    proc = self.proc
                rc = proc.wait()
                depois = le_proc_stat(a.cpu_lista)    # carimbo de uso de CPU no fim do perf
                t_fim = time.time()
                interrompida = int(self.parar.is_set() and (t_fim - t_ini) < a.duracao - 0.5)
                cont = le_contagens(arq)
                w.writerow(linha_metricas(k, t_ini, t_fim, interrompida, rc, cont,
                                          uso_cpu(antes, depois)))
                falhou = rc != 0 and not interrompida
                if falhou:
                    ev.write(f"{time.time():.3f} perf terminou com codigo {rc} na janela {k}\n")
                if not falhou and any(v is not None for v, _ in cont.values()):
                    os.remove(arq)    # ja esta consolidado no metricas.csv; o bruto so fica se falhar
                espera = m0 + k * a.periodo - time.monotonic()   # inicio da proxima janela
                if self.parar.wait(timeout=max(espera, 0)):
                    break
            ev.write(f"{time.time():.3f} pare\n")


def faz_handler(col):
    class H(BaseHTTPRequestHandler):
        def _resp(self, cod, txt):
            corpo = (txt + "\n").encode()
            self.send_response(cod)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, fmt, *args):
            print(f"[{agora()}] {self.client_address[0]} {fmt % args}", flush=True)

        def do_POST(self):
            u = urlsplit(self.path)
            if u.path == "/siga":
                rodada = parse_qs(u.query).get("rodada", [None])[0]
                self._resp(*col.siga(rodada))
            elif u.path == "/pare":
                self._resp(200, "parado" if col.pare() else "nada a parar")
            else:
                self._resp(404, "use POST /siga[?rodada=nome] ou POST /pare")

        def do_GET(self):
            if self.path == "/status":
                self._resp(200, "coletando" if col.ativo() else "parado")
            else:
                self._resp(404, "use GET /status")
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cpus", default="0-5", help="CPUs do host medidas pelo perf (padrao: 0-5)")
    ap.add_argument("--duracao", type=int, default=60, help="duracao de cada janela, em s")
    ap.add_argument("--periodo", type=int, default=120,
                    help="intervalo entre os inicios de janelas, em s (>= duracao)")
    ap.add_argument("--porta", type=int, default=8000)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="endereco de escuta; use o IP da interface do cabo direto")
    ap.add_argument("--outdir", default="perf_out")
    ap.add_argument("--eventos", default=EVENTOS,
                    help="lista de eventos do perf (para testar em CPU sem todos os eventos, "
                         "ex.: '{cycles,instructions,L1-dcache-load-misses}')")
    a = ap.parse_args()

    a.cpu_lista = lista_cpus(a.cpus)
    if a.duracao < 1 or a.periodo < a.duracao:
        sys.exit("use --duracao >= 1 e --periodo >= --duracao")

    os.makedirs(a.outdir, exist_ok=True)
    col = Coletor(a)
    srv = ThreadingHTTPServer((a.bind, a.porta), faz_handler(col))
    print(f"[{agora()}] escutando em {a.bind}:{a.porta}  CPUs {a.cpus}  "
          f"janela {a.duracao} s a cada {a.periodo} s  saida em {a.outdir}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        col.pare()
        srv.server_close()


if __name__ == "__main__":
    main()
