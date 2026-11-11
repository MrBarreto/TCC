#!/usr/bin/env python3
"""Executa a bateria de rodadas chamando o clientes.py em sequencia.

Uso:  ./bateria.py [--ns 32 64 128] [--reps 10] [--pausa 60] [--outdir runs]
      [--url http://192.168.0.4/videos/cat.mp4] [--perf-host 192.168.0.2]
Padroes: video em 192.168.0.4 (VM, interface do cabo) e coleta_perf.py em 192.168.0.2 (host).
Use --perf-host "" para rodar sem coleta de perf.

Ordem intercalada: r1 de todos os N, depois r2 de todos os N, e assim por diante
(r1 com 32, 64 e 128 clientes; r2 com 32, 64 e 128; ...). Assim, uma deriva lenta do
ambiente (temperatura, cache, outros processos) nao fica concentrada em um unico N.
Uma rodada cuja pasta ja existe e pulada, entao a bateria pode ser retomada apos uma
interrupcao; apague a pasta da rodada interrompida antes de retomar.
Ctrl+C encerra a rodada em curso (o clientes.py para os players e avisa o /pare) e a bateria.
"""
import argparse
import os
import subprocess
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://192.168.0.4/videos/cat.mp4",
                help="URL do video, com o IP da VM na interface do cabo")
ap.add_argument("--perf-host", default="192.168.0.2", metavar="HOST[:PORTA]",
                help='host que roda coleta_perf.py ("" desativa o perf)')
ap.add_argument("--ns", type=int, nargs="+", default=[32, 64, 128], help="numeros de clientes")
ap.add_argument("--reps", type=int, default=10, help="repeticoes por N")
ap.add_argument("--pausa", type=int, default=60, help="espera entre rodadas, em s")
ap.add_argument("--outdir", default="runs")
args = ap.parse_args()

clientes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clientes.py")
total = args.reps * len(args.ns)
feitas = 0
atual = None
try:
    for r in range(1, args.reps + 1):
        for n in args.ns:
            feitas += 1
            run = f"r{r}"
            atual = os.path.join(args.outdir, f"n{n:02d}_{run}")
            if os.path.exists(atual):
                print(f"[{time.strftime('%H:%M:%S')}] {feitas}/{total}: {atual} ja existe, pulando")
                continue
            cmd = [sys.executable, clientes, str(n), "--run", run, "--url", args.url,
                   "--outdir", args.outdir]
            if args.perf_host:
                cmd += ["--perf-host", args.perf_host]
            print(f"[{time.strftime('%H:%M:%S')}] {feitas}/{total}: n={n} {run}", flush=True)
            rc = subprocess.call(cmd)
            if rc != 0:
                sys.exit(f"clientes.py terminou com codigo {rc} em {atual}; bateria interrompida "
                         f"(apague {atual} antes de retomar)")
            if feitas < total:
                time.sleep(args.pausa)
except KeyboardInterrupt:
    sys.exit(f"\nbateria interrompida durante {atual} (apague essa pasta antes de retomar)")

print(f"[{time.strftime('%H:%M:%S')}] bateria concluida: {total} rodadas em {args.outdir}/")
