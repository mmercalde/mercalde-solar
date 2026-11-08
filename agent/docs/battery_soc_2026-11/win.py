import sys,time,pickle
from load import gateway,agent
# The gateway's clock runs ~12 min slow (agent/docs/xwplus_gen_ac_2026-11.md):
# shift it onto the agent's time, and keep an agent sample only for a minute
# the gateway does not cover, so the overlap is not interleaved.
LAG=12*60
G={t-LAG:v for t,v in gateway(sys.argv[1]).items()}; A=agent(sys.argv[2])
cover={t//60 for t in G}
S=dict(G); S.update({t:v for t,v in A.items() if t//60 not in cover})
ts=sorted(S); print("gateway",len(G),time.ctime(min(G)),time.ctime(max(G)),"agent",len(A),"merged",len(ts))
R=[(t,)+S[t] for t in ts]   # t,v,i,T,soc,genp,pvp,load
pickle.dump(R,open("merged.pkl","wb"))
