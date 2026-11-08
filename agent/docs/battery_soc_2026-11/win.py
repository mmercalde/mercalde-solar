import sys,time,statistics as st,pickle
from load import gateway,agent
CEF=0.98
G=gateway(sys.argv[1]); A=agent(sys.argv[2])
# merge: gateway primary, agent fills minutes gateway lacks
S=dict(A); S.update(G)
ts=sorted(S); print("gateway",len(G),time.ctime(min(G)),time.ctime(max(G)),"agent",len(A),"merged",len(ts))
R=[(t,)+S[t] for t in ts]   # t,v,i,T,soc,genp,pvp,load
pickle.dump(R,open("merged.pkl","wb"))
