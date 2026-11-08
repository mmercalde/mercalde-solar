import sys,os,time
sys.path.insert(0,os.path.expanduser("~/mercalde-solar/agent"))
from datetime import date,timedelta
import config,scrape_gateway as sg
cfg=config.load(); out="/tmp/battwin/csv"
day=date.today(); empty=0; n=0
with sg.Gateway(cfg) as gw:
    while empty<5 and n<800:
        p=f"{out}/{day}.csv"
        if not os.path.exists(p):
            t=gw.chartdata("system","0",day,"minutes")
            h,rows=sg.parse_chart_csv(t)
            if rows: open(p,"w").write(t); empty=0
            else: empty+=1
            time.sleep(0.3)
        n+=1; day-=timedelta(days=1)
print("done back to",day,"files",len(os.listdir(out)))
