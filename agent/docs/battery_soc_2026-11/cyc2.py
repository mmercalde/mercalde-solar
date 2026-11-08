import pickle,time,statistics as st
R=pickle.load(open("merged.pkl","rb")); tops,starts,cumA,cumW=pickle.load(open("tops.pkl","rb"))
f=lambda t:time.strftime("%Y-%m-%d %H:%M",time.localtime(t))
GAP=300; REST=40
true=[t for t in tops if t['minI']<=36]
excl=[t['day'] for t in tops if t['minI']>36]
print("true tops",len(true),"excluded (never tapered below 36 A at >=59.5):",excl)
cyc=[]
for n,t in enumerate(true):
    k0=t['k']; nxt=true[n+1]['k'] if n+1<len(true) else len(R)
    # endpoint: first sample <=52.0 after top, before the next true top
    ke=next((k for k in range(k0,nxt) if R[k][1]<=52.0),None)
    if ke is None: continue
    span=(R[ke][0]-R[k0][0])/3600
    gaps=[R[k][0]-R[k-1][0] for k in range(k0+1,ke+1)]; mg=max(gaps); lost=sum(g for g in gaps if g>GAP)/3600
    chg=sum(max(0,(R[k][2]+R[k-1][2])/2)*(R[k][0]-R[k-1][0])/3600 for k in range(k0+1,ke+1) if R[k][0]-R[k-1][0]<=GAP)
    ah=cumA[k0]-cumA[ke]; wh=cumW[k0]-cumW[ke]
    # last downward crossing of 57.4 before endpoint
    k57=max((k for k in range(k0+1,ke) if R[k][1]<=57.4<R[k-1][1]),default=None)
    ah57=cumA[k57]-cumA[ke] if k57 else None; wh57=cumW[k57]-cumW[ke] if k57 else None
    gen=any(R[k][5]>100 for k in range(k0,ke))
    # band crossings
    edges=[60.0-0.5*i for i in range(17)]
    cross={}
    for k in range(k0,ke+1):
        if abs(R[k][2])<REST or k==ke:
            for e in edges:
                if e not in cross and R[k][1]<=e: cross[e]=k
    c=dict(top=k0,end=ke,span=span,ah=ah,wh=wh,chg=chg,ah57=ah57,wh57=wh57,gen=gen,lost=lost,mg=mg,cross=cross,topv=R[k0][1],endI=R[ke][2],soc0=R[k0][4],soc1=R[ke][4],T=R[k0][3])
    cyc.append(c)
pickle.dump(cyc,open("cyc.pkl","wb"))
print(f"{'top':16} {'V':>5} {'end (<=52.0)':16} {'I_end':>6} {'hrs':>5} {'netAh':>6} {'kWh':>5} {'chgAh':>6} {'57.4->52 Ah':>11} {'kWh':>5} gen lost_h socTop->end")
for c in cyc:
    print(f"{f(R[c['top']][0]):16} {c['topv']:5.2f} {f(R[c['end']][0]):16} {c['endI']:6.0f} {c['span']:5.1f} {c['ah']:6.0f} {c['wh']/1000:5.1f} {c['chg']:6.0f} {c['ah57'] if c['ah57'] is None else round(c['ah57']):>11} {'' if c['wh57'] is None else round(c['wh57']/1000,1):>5} {int(c['gen'])} {c['lost']:.1f} {c['soc0']:.0f}->{c['soc1']:.0f}")
