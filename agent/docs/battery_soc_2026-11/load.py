import csv,glob,os,sqlite3,time
from datetime import datetime
from zoneinfo import ZoneInfo
TZ=ZoneInfo("America/Tijuana")
def gateway(d):
    out={}
    for p in sorted(glob.glob(d+"/*.csv")):
        lines=[l for l in open(p) if l.strip() and not l.startswith("#")]
        r=csv.reader(lines); h=[x.split("(")[0] for x in next(r)]
        ix={k:h.index(k) for k in ["TIME","/SYS/BATT1/V","/SYS/BATT1/I","/SYS/BATT1/T","/SYS/BATT1/SOC","/SYS/GEN/P","/SYS/PV_TOTAL/P","/SYS/LOAD/P"]}
        for row in r:
            try:
                ts=int(datetime.strptime(row[0],"%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
                v=float(row[ix["/SYS/BATT1/V"]])/1000
                if v<=0: continue
                out[ts]=(v,float(row[ix["/SYS/BATT1/I"]])/1000,float(row[ix["/SYS/BATT1/T"]])/1000,float(row[ix["/SYS/BATT1/SOC"]]),
                         float(row[ix["/SYS/GEN/P"]] or 0),float(row[ix["/SYS/PV_TOTAL/P"]] or 0),float(row[ix["/SYS/LOAD/P"]] or 0))
            except (ValueError,IndexError): pass
    return out
def agent(db):
    c=sqlite3.connect(db)
    return {r[0]:(r[1],r[2],None,r[3],1000.0 if (r[4]==9 or r[5]==9) else 0.0,r[6] or 0,None)
            for r in c.execute("select ts,battery_v,batt_current,batt_soc,mep_action,kub_action,coalesce(mppt80_pv,0)+coalesce(south_pv,0)+coalesce(west_pv,0) from samples order by ts")}
