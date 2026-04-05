import subprocess
import multiprocessing

def play_video(process_id):
    comando = [
        "mplayer", "-msglevel", "all=6", "-v", 
        "-ao", "null", "-vo", "null", 
        "http://192.168.122.142/videos/cat.mp4"
    ]
    print(f"[WORKER] MPlayer {process_id} iniciado...")
    with open(f"mplayer{process_id}.log", "w") as log_file:
        subprocess.run(comando, stdout=log_file, stderr=subprocess.STDOUT)
    print("[WORKER] MPlayer finalizado.")

if __name__ == "__main__":
    processes = []
    for i in range(10):
        process = multiprocessing.Process(target=play_video, args=(i, ))
        processes.append(process)
        process.start()
    for process in processes:
        process.join() 