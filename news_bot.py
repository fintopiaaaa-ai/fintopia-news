"""Fintopia news bot — runs on GitHub Actions (24/7 cloud, no laptop/app).

Every run: fetch many Indian-market RSS feeds, keep only FRESH items (last N min),
drop tip/target headlines (SEBI compliance), score for market significance, skip
anything already posted (dedup via state/posted.json), and post each new qualifying
item to Telegram with a branded card + source attribution. Posts promptly (not in
batches) because it runs every ~15 min and posts each new item as it appears.
"""
import os, io, re, json, time, hashlib, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL=os.environ["TELEGRAM_CHANNEL"]
FRESH_MIN=int(os.environ.get("FRESH_MIN","75"))      # only items newer than this
MIN_SCORE=int(os.environ.get("MIN_SCORE","4"))
MAX_PER_RUN=int(os.environ.get("MAX_PER_RUN","4"))
MAX_PER_DAY=int(os.environ.get("MAX_PER_DAY","16"))
DRY=os.environ.get("DRY","0")=="1"
GEMINI_KEY=os.environ.get("GEMINI_API_KEY","")
GEMINI_MODEL=os.environ.get("GEMINI_MODEL","gemini-2.0-flash")
STATE="state/posted.json"
IST=timezone(timedelta(hours=5,minutes=30))

SOURCES=[
 # Google News RSS search = aggregates ALL publishers, fast, freshness-windowed (robust primary)
 ("GoogleNews","https://news.google.com/rss/search?q=(Nifty%20OR%20Sensex%20OR%20%22Bank%20Nifty%22%20OR%20%22Indian%20stock%20market%22)%20when:3h&hl=en-IN&gl=IN&ceid=IN:en"),
 ("GoogleNews","https://news.google.com/rss/search?q=(RBI%20OR%20SEBI%20OR%20inflation%20OR%20%22repo%20rate%22%20OR%20rupee%20OR%20%22Indian%20economy%22%20OR%20GDP)%20when:3h&hl=en-IN&gl=IN&ceid=IN:en"),
 ("GoogleNews","https://news.google.com/rss/search?q=(FII%20OR%20DII%20OR%20crude%20OR%20%22US%20Fed%22%20OR%20Nasdaq%20OR%20%22global%20markets%22)%20when:3h&hl=en-IN&gl=IN&ceid=IN:en"),
 ("CNBC-TV18","https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
 ("Investing.com","https://www.investing.com/rss/news_285.rss"),
 ("Moneycontrol","https://www.moneycontrol.com/rss/latestnews.xml"),
 ("Economic Times","https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
 ("LiveMint","https://www.livemint.com/rss/markets"),
 ("BusinessLine","https://www.thehindubusinessline.com/markets/feeder/default.rss"),
]
# --- compliance: never post tips/targets/picks ---
BLOCK=re.compile(r"(shares?|stocks?|scrips?)\s+to\s+(buy|sell|accumulate)|top\s+(buy|buys|pick|picks)|"
 r"should\s+you\s+(buy|sell)|must\s+buy|strong\s+buy|buy\s+(now|call|rating)|sell\s+call|best\s+stocks?|"
 r"multibagger|(price\s+target|target\s+price)|stocks?\s+to\s+watch|hot\s+stock|accumulate|book\s+profit|"
 r"stop\s+loss|\brecommend|trading\s+idea|trade\s+setup|intraday\s+(pick|call)|buy\s+the\s+dip|"
 r"suggest(s|ed)?\b.{0,60}\b(buy|sell)\b|\b(buy|sell|hold)\s+(call|rating|recommendation)|"
 r"to\s+(buy|sell)\s+(this|next|today|now)|picks?\s+(for|of)\s+(the\s+)?(week|day|month|today)|"
 r"\bbets?\b.{0,30}(stock|share|nifty)|(stock|share)s?\s+in\s+focus\s+to",re.I)
# --- low-value fluff to skip ---
FLUFF=re.compile(r"\btop\s+\d|things\s+to\s+know|watchlist|what\s+to\s+expect|buzzing|f&o\s+ban|"
 r"muhurat|horoscope|\bwebinar|\bquiz\b|listicle|in\s+pics|\bphotos\b|zodiac",re.I)
# --- significance: entities + magnitude ---
ENT={3:["rbi","sebi","fed","fomc","repo rate","monetary policy","budget","gdp","inflation","cpi","wpi","iip"],
     2:["nifty","sensex","bank nifty","fii","dii","rupee","crude","brent","us fed","jobs data","earnings"],
     1:["market","sensex","stocks","index","sector","ipo","results","economy","global"]}
MAG=re.compile(r"\d+(\.\d+)?\s*%|\d{2,}\s*(points|pts)|record\s+(high|low)|all-?time\s+(high|low)|"
 r"(jumps|surges|soars|slumps|tumbles|crashes|rallies|plunges|gains|falls|rises|drops|spikes)\s+\d|"
 r"\d+-(year|month|week)\s+(high|low)|\bcrore|\blakh\s+crore",re.I)

def fetch(u):
    return urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=20).read()
def parse(x,label):
    out=[]
    try: root=ET.fromstring(x)
    except Exception: return out
    is_g=(label=="GoogleNews")
    for it in root.iter("item"):
        t=(it.findtext("title") or "").strip(); link=(it.findtext("link") or "").strip()
        desc=re.sub("<[^>]+>","",(it.findtext("description") or "")).strip(); pub=it.findtext("pubDate") or ""
        source=label
        if is_g:
            se=it.find("source")
            if se is not None and (se.text or "").strip(): source=se.text.strip()
            # strip trailing " - Publisher" that Google appends to titles
            if " - " in t: source2=t.rsplit(" - ",1)[1].strip(); t=t.rsplit(" - ",1)[0].strip()
        dt=None
        for f in ("%a, %d %b %Y %H:%M:%S %z","%a, %d %b %Y %H:%M:%S %Z","%a, %d %b %Y %H:%M %z"):
            try: dt=datetime.strptime(pub.strip(),f); break
            except Exception: pass
        out.append((t,link,desc,dt,source))
    return out
def score(t,d):
    txt=f"{t} {d}".lower()
    if BLOCK.search(txt) or FLUFF.search(txt): return 0
    s=0
    for w,ws in ENT.items():
        for k in ws:
            if k in txt: s+=w
    if MAG.search(txt): s+=3            # magnitude = genuine market move
    return s
def load_state():
    try: return json.load(open(STATE,encoding="utf-8"))
    except Exception: return {"posted":[],"day":"","count":0}
def save_state(st):
    os.makedirs(os.path.dirname(STATE),exist_ok=True)
    json.dump(st,open(STATE,"w",encoding="utf-8"),indent=0)

def font(b,s):
    p="DejaVuSans-Bold.ttf" if b else "DejaVuSans.ttf"
    for cand in (p,"/usr/share/fonts/truetype/dejavu/"+p):
        try: return ImageFont.truetype(cand,s)
        except Exception: pass
    return ImageFont.load_default()
def wrap(d,t,f,mw):
    w=t.split(); L=[]; c=""
    for x in w:
        if d.textlength((c+" "+x).strip(),font=f)<=mw: c=(c+" "+x).strip()
        else: L.append(c); c=x
    if c: L.append(c)
    return L
def card(h,src):
    W,H=1200,675; TEAL=(0,220,180); WHITE=(240,245,252); GREY=(150,162,182); AMBER=(255,196,61)
    img=Image.new("RGB",(W,H),(10,14,24)); d=ImageDraw.Draw(img)
    for y in range(H):
        tt=y/H; d.line([(0,y),(W,y)],fill=tuple(int((10,14,24)[i]+((6,9,15)[i]-(10,14,24)[i])*tt) for i in range(3)))
    import random; random.seed(len(h)); last=H-90
    for i in range(26):
        x=120+i*40; o=max(360,min(H-60,last+random.randint(-30,30))); c=max(360,min(H-60,o+random.randint(-40,40)))
        col=(38,214,120) if c<o else (235,80,95); d.rectangle([x-7,min(o,c),x+7,max(o,c)],fill=(col[0]//3,col[1]//3,col[2]//3)); last=c
    d.rectangle([0,0,W,8],fill=TEAL); d.rectangle([0,H-8,W,H],fill=TEAL)
    fb=font(True,40); d.text((50,44),"Fintopia",font=fb,fill=WHITE); lw=d.textlength("Fintopia",font=fb); d.text((50+lw+2,44),".",font=fb,fill=TEAL)
    d.text((50,98),"MARKET NEWS  •  EDUCATIONAL CONTEXT ONLY",font=font(True,20),fill=AMBER)
    hf=font(True,46); y=205
    for ln in wrap(d,h,hf,W-100)[:5]: d.text((50,y),ln,font=hf,fill=WHITE); y+=62
    d.text((50,H-120),"Source: "+src,font=font(True,26),fill=TEAL)
    d.text((50,H-78),"Not investment advice. Shared only to inform.",font=font(False,24),fill=GREY)
    b=io.BytesIO(); img.save(b,"JPEG",quality=90); b.seek(0); return b
def gemini_pick(cands):
    """SME layer: pick the single most meaningful + compliant item and write a
    2-sentence educational context. Returns (index, caption) or (None, None)."""
    if not GEMINI_KEY or not cands: return None, None
    lst="\n".join(f"{i}. [{c[3]}] {c[1]}" for i,c in enumerate(cands[:12]))
    prompt=("You are a SEBI-compliance-aware financial news editor for Fintopia, an algorithmic-trading EDUCATION "
      "channel for Indian markets. From the numbered headlines, choose the SINGLE most MEANINGFUL and market-"
      "significant one for an educational audience (major index moves with magnitude, RBI/US Fed/monetary policy, "
      "key economic data, big global/crude/geopolitical events, SEBI/market-structure news). "
      "REJECT (do not choose) anything that is a buy/sell/hold recommendation, target price, stock tip, 'where "
      "analyst X is betting/buying', or low-value fluff. Then write a NEUTRAL 2-sentence educational context for it "
      "explaining why it matters to a systematic/algorithmic process \u2014 with NO advice, NO predictions, NO "
      "buy/sell language. Return STRICT JSON only: {\"index\": <number, or -1 if none suitable>, \"caption\": "
      "\"<two sentences>\"}.\n\nHeadlines:\n"+lst)
    body=json.dumps({"contents":[{"parts":[{"text":prompt}]}],
        "generationConfig":{"temperature":0.3,"response_mime_type":"application/json"}}).encode()
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        r=urllib.request.urlopen(urllib.request.Request(url,data=body,headers={"Content-Type":"application/json"}),timeout=30)
        d=json.load(r); txt=d["candidates"][0]["content"]["parts"][0]["text"]
        obj=json.loads(txt); idx=int(obj.get("index",-1)); cap=(obj.get("caption") or "").strip()
        if idx is None or idx<0 or idx>=len(cands): return None,None
        return idx,cap
    except Exception:
        return None,None

def post(h,src,link,framing=None):
    import uuid
    ctx=framing if framing else "Shared purely for educational market context."
    cap=("⚠️ Education only - not investment advice.\n\n\U0001f4f0 "+h+"\n\n"+ctx+
         "\n\nSource: "+src+" - "+link+"\n\nNot a recommendation to buy, sell, or hold.")
    if len(cap)>1024: cap=cap[:1021].rstrip()+"…"   # Telegram photo-caption hard limit
    bd="----fin"+uuid.uuid4().hex
    def p(n,v): return (f'--{bd}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n').encode()
    body=p("chat_id",CHANNEL)+p("caption",cap)
    body+=(f'--{bd}\r\nContent-Disposition: form-data; name="photo"; filename="n.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode()+card(h,src).read()+f'\r\n--{bd}--\r\n'.encode()
    r=urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",data=body,headers={"Content-Type":f"multipart/form-data; boundary={bd}"})
    try:
        return json.load(urllib.request.urlopen(r,timeout=60)).get("ok")
    except urllib.error.HTTPError as e:
        try: detail=e.read().decode("utf-8","replace")[:300]
        except Exception: detail=""
        print(f"[post] Telegram HTTP {e.code} for '{h[:60]}': {detail}")
        return False
    except Exception as e:
        print(f"[post] Telegram error for '{h[:60]}': {e}")
        return False

def main():
    st=load_state(); today=datetime.now(IST).date().isoformat()
    if st.get("day")!=today: st["day"]=today; st["count"]=0
    seen=set(st.get("posted",[])); now=datetime.now(IST); cands=[]
    for label,u in SOURCES:
        try: items=parse(fetch(u),label)
        except Exception: continue
        for t,link,d,dt,source in items:
            if not link or dt is None: continue
            age=(now-dt.astimezone(IST)).total_seconds()/60
            if age>FRESH_MIN or age< -5: continue
            hid=hashlib.md5(link.encode()).hexdigest()
            if hid in seen: continue
            sc=score(t,d)
            if sc>=MIN_SCORE: cands.append((sc,t,link,source,hid))
    cands.sort(reverse=True)
    posted=[]
    # SME layer: let Gemini choose the single most meaningful + write the caption
    gi,gcap=gemini_pick(cands)
    if gi is not None:
        sc,t,link,source,hid=cands[gi]
        if st["count"]<MAX_PER_DAY:
            if DRY: posted.append((t,source,sc,gcap)); seen.add(hid)
            elif post(t,source,link,gcap): posted.append((t,source,sc,gcap)); seen.add(hid); st["count"]+=1
    else:
        # fallback (no key / API down): post top-scored items with default caption
        for sc,t,link,source,hid in cands:
            if len(posted)>=MAX_PER_RUN or st["count"]>=MAX_PER_DAY: break
            if DRY: posted.append((t,source,sc,None)); seen.add(hid); continue
            if post(t,source,link): posted.append((t,source,sc,None)); seen.add(hid); st["count"]+=1; time.sleep(1)
    st["posted"]=list(seen)[-600:]
    if not DRY: save_state(st)
    print(json.dumps({"checked_feeds":len(SOURCES),"fresh_candidates":len(cands),
                      "posted":[{"title":t,"source":s,"score":sc,"caption":cap} for t,s,sc,cap in posted]},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
