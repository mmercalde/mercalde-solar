import pickle,statistics as st,time
R=pickle.load(open("merged.pkl","rb")); cyc=pickle.load(open("cyc.pkl","rb"))
edges=[round(60.0-0.5*i,1) for i in range(17)]
def run(rows_iter,REST=40):
    band={}
    for s in rows_iter:
        ah=wh=0; cross={}
        for k,r in enumerate(s):
            if k:
                dt=r[0]-s[k-1][0]; i=(r[2]+s[k-1][2])/2; ah-=i*dt/3600*(0.98 if i>0 else 1); wh-=(r[1]*r[2]+s[k-1][1]*s[k-1][2])/2*dt/3600*(0.98 if i>0 else 1)
            if abs(r[2])<REST:
                for e in edges:
                    if e not in cross and r[1]<=e: cross[e]=(ah,wh)
        v0=s[0][1]
        for hi,lo in zip(edges,edges[1:]):
            if hi in cross and lo in cross and v0>hi:
                band.setdefault(hi,[]).append((cross[lo][0]-cross[hi][0],cross[lo][1]-cross[hi][1]))
    return band
# night segments over full 18 months
segs=[];cur=[]
for r in R:
    ok=r[6]<50 and r[5]<=100 and r[2]<5
    if ok and cur and r[0]-cur[-1][0]<=300: cur.append(r)
    else:
        if len(cur)>10: segs.append(cur)
        cur=[r] if ok else []
B1=run(segs)
# the 5 full cycles (charge between counted, CEF 0.98)
B2=run([R[c['top']:c['end']+1] for c in cyc])
print(f"{'band':10} {'nights n':>8} {'Ah':>5} {'IQR':>11} {'Wh':>5} | {'5 cycles n':>10} {'Ah':>5}")
c1=c2=0
for hi in edges[:-1]:
    v=B1.get(hi,[]); u=B2.get(hi,[])
    a=st.median(x[0] for x in v) if v else float('nan'); w=st.median(x[1] for x in v) if v else float('nan')
    q=st.quantiles([x[0] for x in v],n=4) if len(v)>1 else [a,a,a]
    b=st.median(x[0] for x in u) if u else float('nan')
    print(f"{hi:.1f}-{hi-0.5:.1f}  {len(v):8d} {a:5.0f} [{q[0]:4.0f}..{q[2]:4.0f}] {w:5.0f} | {len(u):10d} {b:5.0f}")
print()
for lo_top in (59.5,57.5):
    A=sum(st.median(x[0] for x in B1[h]) for h in edges[:-1] if h<=lo_top); W=sum(st.median(x[1] for x in B1[h]) for h in edges[:-1] if h<=lo_top)
    print(f"night-band sum {lo_top}->52.0: {A:.0f} Ah {W/1000:.1f} kWh")
# 0.5V-band cumulative from 52 up (for the curve table)
cum=0;cw=0
for h in reversed(edges[:-1]):
    if h>59.5: continue
    cum+=st.median(x[0] for x in B1[h]); cw+=st.median(x[1] for x in B1[h])
    print(f"52.0->{h:.1f}: {cum:.0f} Ah {cw/1000:.1f} kWh")
# 57.4 vs 57.5: interpolate 57.5-57.0 band
