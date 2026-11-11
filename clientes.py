#!/usr/bin/env python3
"""Dispara N clientes mpv em paralelo; cada um grava o seu .log bruto.

Uso:  ./clientes.py 16 --run r1 [--perf-host 10.0.0.1]
Saida: <outdir>/n16_r1/p01.log ... p16.log, t0.txt e launch.txt (instantes, epoch)
Cada linha do .log comeca com a hora (epoch, segundos). Requer: sudo apt install moreutils
Ctrl+C encerra todos os players.
Registra tambem o uso de recursos desta maquina durante a rodada (1 amostra por segundo) em
recursos.csv: cpu_pct (media de todas as CPUs), cpu_max_pct (CPU mais ocupada), mem_usada_mib,
mem_pct (memoria em uso, sem contar cache) e rede_rx_mbps (trafego recebido, exceto loopback).

Com --perf-host, avisa o host da VM (coleta_perf.py, porta 8000 se omitida) quando a rodada
comeca (POST /siga?rodada=n16_r1, e a pasta do host leva esse nome) e quando termina
(POST /pare), para ele coletar o perf stat durante a rodada. Os instantes das mensagens
ficam em perf.txt. Se o /siga falhar, a rodada nao comeca.
"""
import argparse
import csv
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import psutil

ap = argparse.ArgumentParser()
ap.add_argument("n", type=int, help="numero de clientes")
ap.add_argument("--run", default="r1", help="rotulo da repeticao (r1..r5)")
ap.add_argument("--url", default="http://192.168.0.4/videos/cat.mp4")
ap.add_argument("--outdir", default="runs")
ap.add_argument("--perf-host", default=None, metavar="HOST[:PORTA]",
                help="host que roda coleta_perf.py (omitido: nao coleta perf)")
args = ap.parse_args()

if shutil.which("ts") is None:
    sys.exit("falta o comando 'ts': sudo apt install moreutils")

perf_host = args.perf_host
if perf_host and ":" not in perf_host:
    perf_host += ":8000"


def avisa(msg):
    """POST vazio em http://<perf-host>/<msg>; devolve o texto da resposta."""
    req = urllib.request.Request(f"http://{perf_host}/{msg}", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    except urllib.error.HTTPError as e:   # mostra o motivo enviado pelo host
        raise OSError(f"HTTP {e.code}: {e.read().decode().strip()}") from None


def monitora(caminho, parar, intervalo=1.0):
    """Grava em `caminho` o uso de CPU, memoria e rede a cada `intervalo` s, ate `parar` ser acionado."""
    def rx_bytes():
        return sum(v.bytes_recv for k, v in psutil.net_io_counters(pernic=True).items() if k != "lo")

    with open(caminho, "w", newline="", buffering=1) as f:
        w = csv.writer(f)
        w.writerow(["epoch", "cpu_pct", "cpu_max_pct", "mem_usada_mib", "mem_pct", "rede_rx_mbps"])
        psutil.cpu_percent(interval=None, percpu=True)     # marca o inicio da primeira amostra
        t_ant, rx_ant = time.time(), rx_bytes()
        while not parar.wait(intervalo):
            t, rx = time.time(), rx_bytes()
            por_cpu = psutil.cpu_percent(interval=None, percpu=True)
            vm = psutil.virtual_memory()
            w.writerow([f"{t:.3f}", round(sum(por_cpu) / len(por_cpu), 2), max(por_cpu),
                        round((vm.total - vm.available) / 2**20, 1), vm.percent,
                        round((rx - rx_ant) * 8 / (t - t_ant) / 1e6, 2)])
            t_ant, rx_ant = t, rx


pasta = os.path.join(args.outdir, f"n{args.n:02d}_{args.run}")
os.makedirs(pasta)  # erro se ja existir: nao sobrescreve rodada anterior

if perf_host:
    try:
        sessao = avisa("siga?rodada=" + os.path.basename(pasta))
    except OSError as e:  # inclui HTTPError (ex.: 409, ja ha coleta em curso)
        os.rmdir(pasta)
        sys.exit(f"falha ao enviar /siga para {perf_host}: {e}")
    with open(os.path.join(pasta, "perf.txt"), "w") as f:
        f.write(f"siga {time.time():.3f} {sessao}\n")
    print(f"perf: coleta iniciada em {perf_host} ({sessao})")
else:
    print("perf: desativado (use --perf-host para coletar no host da VM)")

# O mpv escreve a linha de status com \r e nao grava hora. O pipeline abaixo troca
# \r por \n e o `ts` coloca o epoch (s, com microssegundos) no inicio de cada linha.
# O `stdbuf -o0` evita buffer entre os programas: sem ele os carimbos saem em
# rajadas e nao refletem o tempo real.
#
# mpv por padrao guardaria o video inteiro (cache-secs=3600000, 150 MiB): o buffer e
# limitado a 30 s de video. Espera 15 s de video antes de iniciar e antes de retomar
# apos um travamento.
STATUS = "pos=${=time-pos} cache_s=${=demuxer-cache-duration} paused_for_cache=${=paused-for-cache}"
mpv = " ".join([
    "mpv --no-config --vo=null --no-audio",
    "--cache=yes --cache-secs=30 --demuxer-max-bytes=64MiB --demuxer-max-back-bytes=0",
    "--cache-pause-initial=yes --cache-pause-wait=15",
    shlex.quote("--term-status-msg=" + STATUS),
    shlex.quote(args.url)])

t0 = time.time()
parar_monitor = threading.Event()
monitor = threading.Thread(target=monitora, args=(os.path.join(pasta, "recursos.csv"), parar_monitor),
                           daemon=True)
monitor.start()
try:
    with open(os.path.join(pasta, "t0.txt"), "w") as f:
        f.write(f"{t0:.3f}\n")

    procs = []
    lancamentos = []  # instante (epoch) em que cada player foi lancado
    for i in range(1, args.n + 1):
        log = shlex.quote(os.path.join(pasta, f"p{i:02d}.log"))
        pipeline = f"{mpv} 2>&1 | stdbuf -o0 tr '\\r' '\\n' | ts '%.s' > {log}"
        lancamentos.append(time.time())
        procs.append(subprocess.Popen(pipeline, shell=True, stdin=subprocess.DEVNULL,
                                      start_new_session=True))
    with open(os.path.join(pasta, "launch.txt"), "w") as f:
        for i, t in enumerate(lancamentos, 1):
            f.write(f"p{i:02d} {t:.3f}\n")
    print(f"{args.n} players lancados; logs em {pasta}/ (Ctrl+C encerra)")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:           # cada pipeline esta no seu proprio grupo de processos
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        for p in procs:
            p.wait()
finally:
    parar_monitor.set()
    monitor.join(timeout=5)
    if perf_host:                 # o /pare sai mesmo com Ctrl+C ou erro no lancamento
        try:
            resp = avisa("pare")
            with open(os.path.join(pasta, "perf.txt"), "a") as f:
                f.write(f"pare {time.time():.3f} {resp}\n")
            print(f"perf: {resp}")
        except OSError as e:
            print(f"AVISO: falha ao enviar /pare para {perf_host}: {e}; "
                  f"mande manualmente: curl -X POST http://{perf_host}/pare", file=sys.stderr)

print(f"fim em {time.time() - t0:.1f} s")
