import pickle,time,statistics as st,sys
R=pickle.load(open("merged.pkl","rb"))
W=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[2].isdigit() else 775; CEF=float(sys.argv[1]) if len(sys.argv)>1 else 0.98; GAP=300
f=lambda t:time.strftime("%Y-%m-%d %H:%M",time.localtime(t))
ah=None; last=None; run=0; armed_bottom=True; ev=[]; thr=0; above=0
for k in range(1,len(R)):
    a,b=R[k-1],R[k]; dt=b[0]-a[0]
    if ah is not None and dt<=GAP:
        i=(a[2]+b[2])/2*dt/3600; ah+= i*CEF if i>0 else i; thr+=abs(i)
    run = run+dt if (b[1]>=59.5 and 0<=b[2]<36 and dt<=GAP) else 0
    if b[1]>52.3: above+=dt
    if run>=240:
        if ah is not None and (last is None or last[0]!="top" or b[0]-last[1]>6*3600): ev.append(("top",b[0],ah-W,thr,(b[0]-last[1])/3600 if last else 0))
        if last is None or last[0]!="top" or b[0]-last[1]>6*3600: thr=0
        ah=W; last=("top",b[0]); run=0
    if b[1]<=52.0<a[1] and above>=7200:
        if ah is not None: ev.append(("bottom",b[0],ah-0,thr,(b[0]-last[1])/3600))
        ah=0; last=("bottom",b[0]); thr=0
    if b[1]<=52.0: above=0
def rep(kind):
    e=[x for x in ev if x[0]==kind and x[4]>0]
    err=[x[2] for x in e]
    print(f"{kind}: n={len(e)} error Ah median {st.median(err):.0f} IQR {[round(q) for q in st.quantiles(err,n=4)]} |err|<=39Ah(5%): {sum(abs(x)<=39 for x in err)}/{len(err)}")
    return e
print("CEF",CEF)
t=rep("top"); b=rep("bottom")
# error per kAh throughput for bottom anchors
print("bottom error vs days since anchor:")
for lo,hi in ((0,1),(1,3),(3,7),(7,30),(30,999)):
    e=[x[2] for x in b if lo<=x[4]/24<hi]
    if e: print(f"  {lo}-{hi} d: n={len(e)} median {st.median(e):.0f} Ah, IQR {[round(q) for q in st.quantiles(e,n=4)] if len(e)>1 else e}")
if "-v" in sys.argv:
    for x in ev[-25:]: print(x[0],f(x[1]),round(x[2]),"thr",round(x[3]),"h",round(x[4],1))
