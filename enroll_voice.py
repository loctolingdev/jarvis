from pathlib import Path
import wave, numpy as np, sounddevice as sd, time
ROOT=Path(__file__).resolve().parent
RATE=16000; SECONDS=8
print('Enregistrement du profil vocal dans 3 secondes.')
print("Parle naturellement pendant 8 secondes, par exemple : Salut Jarvis, ceci est ma voix et je suis loctoling.")
for n in (3,2,1): print(n); time.sleep(1)
print('Parle maintenant...')
a=sd.rec(RATE*SECONDS,samplerate=RATE,channels=1,dtype='int16'); sd.wait()
rms=float(np.sqrt(np.mean((a.astype(np.float64)/32768)**2)))
if rms<0.008:
 print('ERREUR : enregistrement trop silencieux. Rapproche-toi du micro et recommence.'); input('Entrée pour fermer...'); raise SystemExit(1)
with wave.open(str(ROOT/'voice_profile.wav'),'wb') as w:
 w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(a.tobytes())
print('Profil vocal enregistré localement. Jarvis filtrera maintenant les autres voix.')
input('Entrée pour fermer...')
