import subprocess
import multiprocessing
import time
import math
import random

def play_video(process_id):
    comando = [
        "mplayer", "-msglevel", "all=6", "-v", 
        "-ao", "null", "-vo", "null", 
        "http://192.168.122.142/videos/cat.mp4?client_id={process_id}"
    ]
    print(f"[WORKER] MPlayer {process_id} iniciado...")
    with open(f"mplayer{process_id}.log", "w") as log_file:
        subprocess.run(comando, stdout=log_file, stderr=subprocess.STDOUT)

if __name__ == "__main__":
    N_CLIENTES = 10
    LAMBDA_MIN = 10
    SEED = 1
    
    random.seed(SEED)
    lambda_sec = LAMBDA_MIN / 60.0
    
    cronograma = []
    tempo_acumulado = 0
    
    for i in range(N_CLIENTES):
        u = 1.0 - random.random()
        intervalo = -math.log(u) / lambda_sec 
        tempo_acumulado += intervalo
        cronograma.append(tempo_acumulado)

    print(f"[MASTER] Cronograma de disparos (segundos): {[round(t, 2) for t in cronograma]}")
    
    inicio_experimento = time.time()
    processes = []
    
    for i in range(N_CLIENTES):
        instante_alvo = cronograma[i]
        
        tempo_agora = time.time() - inicio_experimento
        espera_necessaria = instante_alvo - tempo_agora
        
        if espera_necessaria > 0:
            time.sleep(espera_necessaria)
        
        p = multiprocessing.Process(target=play_video, args=(i,))
        processes.append(p)
        p.start()
        print(f"[MASTER] Processo {i} disparado em T + {time.time() - inicio_experimento:.2f}s")

    for p in processes:
        p.join()