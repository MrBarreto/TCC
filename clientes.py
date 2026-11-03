#!/usr/bin/env python3
"""Dispara N clientes mpv em paralelo; cada um grava o seu .log bruto.

Uso:  ./clientes.py 16 --run r1
Saida: <outdir>/n16_r1/p01.log ... p16.log  e  t0.txt (instante de inicio, epoch)
Cada linha do .log comeca com a hora (epoch, segundos). Requer: sudo apt install moreutils
Ctrl+C encerra todos os players.
"""
import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("n", type=int, help="numero de clientes")
ap.add_argument("--run", default="r1", help="rotulo da repeticao (r1..r5)")
ap.add_argument("--url", default="http://192.168.122.30/videos/cat.mp4")
ap.add_argument("--outdir", default="runs")
args = ap.parse_args()

if shutil.which("ts") is None:
    sys.exit("falta o comando 'ts': sudo apt install moreutils")

pasta = os.path.join(args.outdir, f"n{args.n:02d}_{args.run}")
os.makedirs(pasta)  # erro se ja existir: nao sobrescreve rodada anterior

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

print(f"fim em {time.time() - t0:.1f} s")
