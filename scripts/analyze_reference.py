import numpy as np, librosa, scipy.signal as sig, scipy.ndimage as ndi, json
from pathlib import Path
import argparse
p = argparse.ArgumentParser()
p.add_argument('--audio', default=str(Path(__file__).resolve().parents[1] / 'assets/reference_audio.wav'))
p.add_argument('--out', default=str(Path.cwd() / 'features_recomputed.json'))
args = p.parse_args()
p=args.audio
y,sr=librosa.load(p,sr=11025)
hop=256
rms=librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
# Fourier spectrum for chroma/centroid
S=np.abs(librosa.stft(y,n_fft=2048,hop_length=hop))
cent=librosa.feature.spectral_centroid(S=S,sr=sr)[0]
onset=librosa.onset.onset_strength(S=librosa.amplitude_to_db(S,ref=np.max),sr=sr,hop_length=hop)
sec=librosa.frames_to_time(np.arange(len(rms)),sr=sr,hop_length=hop)
# smooth 2 sec RMS and onset, normalize 5-95 quantile
m=ndi.gaussian_filter1d(np.asarray(rms), sigma=1.8*sr/hop)
on=ndi.gaussian_filter1d(onset, sigma=1.3*sr/hop)
ce=ndi.gaussian_filter1d(cent, sigma=1.8*sr/hop)
# normalization
norm=lambda x: np.clip((x-np.percentile(x,10))/(np.percentile(x,95)-np.percentile(x,10)+1e-10),0,1)
mr=norm(m); mo=norm(on); mc=norm(ce)
# activity with onset and rms (this is an engineered proxy not emotion)
energy=0.7*mr+0.3*mo
activity=ndi.gaussian_filter1d(energy, sigma=0.7*sr/hop)
# local peaks separated at least 10 sec with prominence
peaks,_=sig.find_peaks(activity, distance=int(10*sr/hop), prominence=0.10)
idx=sorted(peaks,key=lambda i:activity[i],reverse=True)
print('duration',len(y)/sr)
print('global 10 strongest peaks',[(round(float(sec[i]),1),round(float(activity[i]),2)) for i in idx[:10]])
# 5s windows value and sorted top window
N=5
agg=[]
for start in range(0,165,5):
 mask=(sec>=start)&(sec<start+N)
 agg.append({'start':start,'end':start+5,'rms':round(float(np.mean(mr[mask])),3),'onsets':round(float(np.mean(mo[mask])),3),'centroid':round(float(np.mean(mc[mask])),3),'activity':round(float(np.mean(activity[mask])),3)})
print('top 5s',sorted(agg,key=lambda x:x['activity'],reverse=True)[:9])
print('bottom 5s',sorted(agg,key=lambda x:x['activity'])[:9])
print('timeline 10s',[(x['start'],x['activity']) for x in agg if x['start']%10==0])
# 20s groups
print('sections by 15s:')
for start in range(0,165,15):
 a=[x['activity'] for x in agg if start<=x['start']<start+15]
 print(start, round(float(np.mean(a)),2))
with open(args.out,'w') as f: json.dump({'duration':float(len(y)/sr),'top_peaks':[(round(float(sec[i]),1),round(float(activity[i]),2)) for i in idx[:10]],'windows_5sec':agg},f,ensure_ascii=False,indent=2)
