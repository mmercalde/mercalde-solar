import pickle,time,statistics as st,sys,collections
R=pickle.load(open("merged.pkl","rb"))  # t,v,i,T,soc,genp,pvp,load
CEF=0.98; GAP=300
f=lambda t:time.strftime("%Y-%m-%d %H:%M",time.localtime(t))
day=lambda t:time.strftime("%Y-%m-%d",time.localtime(t))
# cumulative state (Ah, Wh) with CEF on charge
cumA=[0.0];cumW=[0.0]
for a,b in zip(R,R[1:]):
    dt=b[0]-a[0]
    if dt>GAP: cumA.append(cumA[-1]);cumW.append(cumW[-1]);continue
    i=(a[2]+b[2])/2; p=(a[1]*a[2]+b[1]*b[2])/2
    k=CEF if i>0 else 1.0
    cumA.append(cumA[-1]+k*i*dt/3600); cumW.append(cumW[-1]+k*p*dt/3600)
# true tops: per day, minutes with v>=59.5; need taper: some minute with v>=59.5 and 0<=i<36 for >=4 consecutive min
tops=[]
byday=collections.defaultdict(list)
for k,r in enumerate(R):
    if r[1]>=59.4: byday[day(r[0])].append(k)
for d,ks in sorted(byday.items()):
    peak=max(R[k][1] for k in ks)
    if peak<59.5: continue
    run=0;taper=False;minI=min(R[k][2] for k in ks if R[k][1]>=59.5)
    for k in range(ks[0],ks[-1]+1):
        r=R[k]
        run=run+1 if (r[1]>=59.5 and 0<=r[2]<36) else 0
        if run>=4: taper=True
    kmax=max(ks,key=lambda k:cumA[k])   # state maximum among high-voltage minutes
    gen=any(R[k][5]>100 for k in range(ks[0],ks[-1]+1))
    tops.append(dict(day=d,peak=peak,taper=taper,k=kmax,minI=minI,gen=gen))
print("Days >= 59.5 V:")
for t in tops: print(f"  {t['day']} peak {t['peak']:.2f} V taper={t['taper']} min I at >=59.5: {t['minI']:.0f} A gen_during_top={t['gen']} top point {f(R[t['k']][0])} {R[t['k']][1]:.2f}V {R[t['k']][2]:.0f}A soc{R[t['k']][4]:.0f}")
# 52.0 V generator starts: genp goes >100 after >=10 min at 0, with v <= 52.1 at the minute before
starts=[]
for k in range(10,len(R)):
    if R[k][5]>100 and all(R[j][5]<=100 for j in range(k-10,k)) and R[k-1][1]<=52.1 and R[k][0]-R[k-10][0]<900:
        starts.append(k)
print("\n52.0 V gen starts:",len(starts))
for k in starts: print("  ",f(R[k][0]),R[k-1][1],R[k-1][2],"soc",R[k-1][4])
pickle.dump((tops,starts,cumA,cumW),open("tops.pkl","wb"))
