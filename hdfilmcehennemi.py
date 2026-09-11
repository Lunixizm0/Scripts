#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KULLANIM:
  python3 hdfilmcehennemi.py "https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/"
  python3 hdfilmcehennemi.py "https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/" -o film.mp4
  python3 hdfilmcehennemi.py "URL" --limit 20  # test için sadece ilk 20 segment
"""
import re, sys, os, subprocess, base64, time
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}
TIMEOUT=15

def fetch(url, referer=None):
    h=dict(HEADERS)
    if referer: h["Referer"]=referer
    r=requests.get(url, headers=h, timeout=20)
    if r.status_code==403 or "Just a moment" in r.text[:5000]:
        raise RuntimeError(f"403 Cloudflare - TR proxy gerekir: {url}")
    r.raise_for_status()
    return r.text

def fetch_bytes(url, referer=None):
    h=dict(HEADERS)
    if referer: h["Referer"]=referer
    r=requests.get(url, headers=h, timeout=20)
    r.raise_for_status()
    return r.content

def extract_iframe(html, base):
    m=re.search(r'<iframe[^>]+data-src=["\']([^"\']+)["\']', html, re.I)
    if not m: m=re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if not m: return None
    src=m.group(1)
    if src.startswith("//"): src="https:"+src
    elif src.startswith("/"): src=urljoin(base,src)
    elif not src.startswith("http"): src=urljoin(base+"/",src)
    return src

def decode_via_node(iframe_html):
    scripts=re.findall(r'<script[^>]*>(.*?)</script>', iframe_html, re.S|re.I)
    for scr in scripts:
        if "function" not in scr or "var " not in scr: continue
        m=re.search(r'function\s+(\w+)\s*\(.*?\{.*?return\s+\w+\s*;\s*\}.*?var\s+(\w+)\s*=\s*(\w+)\s*\(\s*\[([^\]]+)\]', scr, re.S)
        if not m: continue
        func_name=m.group(1)
        var_name=m.group(2)
        call_name=m.group(3)
        if func_name!=call_name: continue
        func_match=re.search(r'(function\s+'+re.escape(func_name)+r'\s*\(.*?\{.*?return\s+\w+\s*;\s*\})', scr, re.S)
        if not func_match: continue
        func_def=func_match.group(1)
        arr_content=m.group(4)
        parts=re.findall(r'"([^"]+)"', arr_content)
        if not parts: parts=re.findall(r"'([^']+)'", arr_content)
        if not parts: continue
        js_arr="["+",".join(f'"{p}"' for p in parts)+"]"
        js_code=func_def+f"\nvar __r={func_name}({js_arr});console.log(__r);"
        try:
            res=subprocess.run(["node","-e",js_code], capture_output=True, text=True, timeout=5)
            if res.returncode==0 and res.stdout.strip().startswith("http"):
                return res.stdout.strip()
        except: continue
    return None

def find_master(html):
    u=decode_via_node(html)
    if u and "master" in u: return u
    m=re.search(r'"contentUrl"\s*:\s*"([^"]+)"', html)
    if m and "master" in m.group(1): return m.group(1)
    m=re.search(r'https?://[^\s"\'<>]+master\.[a-z]+', html)
    if m: return m.group(0)
    return None

def parse_master(txt, base):
    audios=[]
    for m in re.finditer(r'#EXT-X-MEDIA:TYPE=AUDIO.*?NAME="([^"]+)".*?URI="([^"]+)"', txt):
        name,uri=m.group(1),m.group(2)
        audios.append((name, urljoin(base, uri)))
    video=None
    m=re.search(r'#EXT-X-STREAM-INF.*\n([^\n]+)', txt)
    if m:
        video=urljoin(base, m.group(1).strip())
    return video, audios

def parse_segments(txt, base):
    segs=[]
    for line in txt.splitlines():
        line=line.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("http"): segs.append(line)
        else: segs.append(urljoin(base, line))
    return segs

def download_parallel(urls, referer, tmpdir, prefix, limit=None):
    os.makedirs(tmpdir, exist_ok=True)
    if limit: urls=urls[:limit]
    print(f"[*] {prefix} {len(urls)} segment indiriliyor (16 thread)...")
    files=[None]*len(urls)
    def dl(idx_url):
        idx,url=idx_url
        for attempt in range(3):
            try:
                data=fetch_bytes(url, referer=referer)
                p=os.path.join(tmpdir, f"{prefix}_{idx:05d}.ts")
                open(p,"wb").write(data)
                return idx,p
            except Exception as e:
                if attempt==2: raise
                time.sleep(0.5)
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs={ex.submit(dl,(i,u)):i for i,u in enumerate(urls)}
        done=0
        for fut in as_completed(futs):
            try:
                idx,p=fut.result()
                files[idx]=p
                done+=1
                if done%100==0 or done==len(urls):
                    print(f"  {done}/{len(urls)}")
            except Exception as e:
                print(f"[!] segment hata: {e}")
    # list file for ffmpeg concat
    list_path=os.path.join(tmpdir, f"{prefix}_list.txt")
    with open(list_path,"w") as f:
        for p in files:
            if p: f.write(f"file '{os.path.abspath(p)}'\n")
    return list_path, files

def extract_title(html):
    m=re.search(r'<h1[^>]*class="section-title"[^>]*>(.*?)</h1>', html, re.S)
    if m:
        t=re.sub(r'<[^>]+>','',m.group(1)).strip()
        t=re.sub(r'\s+',' ',t)
        return t
    m=re.search(r'<title>(.*?)</title>', html, re.S|re.I)
    if m: return re.sub(r'<[^>]+>','',m.group(1)).strip()[:80]
    return "Bilinmeyen Film"

def extract_alternatives(html, base_iframe):
    # film sayfasındaki Close/Rapidrame butonları
    alts=[]
    for m in re.finditer(r'class="alternative-link"[^>]*data-video="([^"]+)"[^>]*>([^<]+)</button>', html):
        vid, name=m.group(1).strip(), m.group(2).strip()
        alts.append((name, vid))
    if not alts:
        # fallback: iframe'i tek kaynak olarak göster
        alts=[("Varsayilan", "")]
    return alts

def build_iframe_url(base_iframe, alt_vid, alt_name):
    # base_iframe örn https://hdfilmcehennemi.mobi/video/embed/kCklCebq05d/?rapidrame_id=a01c...
    # Close için rapidrame_id olmadan, Rapidrame için id ile
    if not alt_vid or alt_name.lower()=="varsayilan":
        return base_iframe
    m=re.search(r'/embed/([^/\?]+)', base_iframe)
    vid=m.group(1) if m else ""
    if "rapidrame" in alt_name.lower():
        return f"https://hdfilmcehennemi.mobi/video/embed/{vid}/?rapidrame_id={alt_vid}"
    else:
        # Close vb: sadece embed id
        return f"https://hdfilmcehennemi.mobi/video/embed/{vid}/"

def extract_subtitles(iframe_html):
    subs=[]
    for m in re.finditer(r'"file"\s*:\s*"(https:[^"]+\.vtt)"[^}]*"label"\s*:\s*"([^"]+)"', iframe_html):
        url,label=m.group(1).replace("\\/","/"), m.group(2)
        subs.append((label, url))
    # fallback basit
    if not subs:
        for m in re.finditer(r'"file":"(https:[^"]+\.vtt)"', iframe_html):
            subs.append(("Bilinmeyen", m.group(1).replace("\\/","/")))
    return subs

def choose_interactive(options, prompt, default=1):
    # options: list of (display, value) veya str
    if not options:
        return None
    print(f"\n{prompt}")
    for i,opt in enumerate(options,1):
        disp=opt[0] if isinstance(opt, tuple) else opt
        print(f"  [{i}] {disp}")
    while True:
        try:
            ans=input(f"Secim [1-{len(options)}] (varsayilan {default}): ").strip()
            if ans=="": ans=str(default)
            idx=int(ans)
            if 1<=idx<=len(options):
                return options[idx-1]
            print("Gecersiz numara")
        except ValueError:
            print("Sayi gir")
        except KeyboardInterrupt:
            print("\nIptal"); sys.exit(0)

def main():
    import argparse
    ap=argparse.ArgumentParser(description="HDFilmCehennemi indir")
    ap.add_argument("url", nargs="?", help="Film URL")
    ap.add_argument("-o","--output", help="Çıkış mp4")
    ap.add_argument("--limit", type=int, help="Test için segment limiti")
    ap.add_argument("-i","--interactive", action="store_true", help="İnteraktif mod")
    ap.add_argument("--no-interactive", action="store_true", help="İnteraktif kapali")
    args=ap.parse_args()

    interactive=args.interactive
    # eğer url verilmemişse interaktif zorunlu
    if not args.url:
        interactive=True

    film_url=args.url
    out=args.output
    limit=args.limit

    # İnteraktif: film URL sor
    if interactive:
        if not film_url:
            film_url=input("Film URL'si (ornek: https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/): ").strip().strip('"').strip("'")
            if not film_url:
                print("URL gerekli"); sys.exit(1)
        # çıkış dosyası sorulacak daha sonra
    else:
        if not film_url:
            print('Kullanım: python3 hdfilmcehennemi.py "URL" [-o çıkış.mp4] [-i interaktif]')
            sys.exit(1)

    if not out and not interactive:
        slug=urlparse(film_url).path.strip("/").split("/")[-1] or "film"
        slug=re.sub(r'[\\/:*?"<>|]','_',slug)[:100]
        out=slug+".mp4"

    print(f"[*] Film: {film_url}")
    base=f"{urlparse(film_url).scheme}://{urlparse(film_url).netloc}"
    print("[*] Sayfa çekiliyor...")
    html=fetch(film_url)
    title=extract_title(html)
    print(f"[+] Başlık: {title}")

    base_iframe=extract_iframe(html, base)
    if not base_iframe:
        print("[-] iframe yok"); sys.exit(1)
    print(f"[+] iframe (ham): {base_iframe}")

    alts=extract_alternatives(html, base_iframe)
    chosen_alt=None
    if interactive:
        # alternatif kaynak seçimi
        if len(alts)>1:
            chosen_alt=choose_interactive([(f"{n} ({'aktif' if i==0 else ''})", (n,v)) for i,(n,v) in enumerate(alts)], "Video kaynağı seç:", default=1)
            alt_name, alt_vid=chosen_alt[1]
            iframe=build_iframe_url(base_iframe, alt_vid, alt_name)
            print(f"[+] Seçilen kaynak: {alt_name} -> {iframe}")
        else:
            iframe=base_iframe
            alt_name=alts[0][0]
    else:
        iframe=base_iframe
        alt_name=alts[0][0] if alts else "Varsayılan"

    print("[*] İframe çekiliyor...")
    iframe_html=fetch(iframe, referer=film_url)
    print("[*] Master çözülüyor...")
    master=find_master(iframe_html)
    if not master:
        print("[-] master yok"); open("/tmp/iframe_debug.html","w").write(iframe_html); sys.exit(1)
    print(f"[+] master: {master}")

    referer="https://hdfilmcehennemi.mobi/"
    print("[*] Master içeriği alınıyor...")
    master_txt=fetch(master, referer=referer)
    if not interactive:
        print(master_txt[:500])
    mbase=master.rsplit("/",1)[0]+"/"

    streams=[]
    for m in re.finditer(r'#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)', master_txt):
        attrs,uri=m.group(1),m.group(2).strip()
        bw=re.search(r'BANDWIDTH=(\d+)', attrs)
        res=re.search(r'RESOLUTION=\d+x(\d+)', attrs)
        streams.append((f'{res.group(1)+"p" if res else "Bilinmeyen"} - {int(bw.group(1))//1000}kbps' if bw else uri, urljoin(mbase, uri), attrs))
    if streams:
        print(f"[+] {len(streams)} kalite bulundu")
        if interactive:
            # kalite seç
            chosen_stream=choose_interactive([(d, (d,u)) for d,u,_ in streams], "Kalite seç:", default=1)
            video_url=chosen_stream[1][1]
            print(f"[+] Seçilen kalite: {chosen_stream[0]} -> {video_url}")
        else:
            streams_sorted=sorted(streams, key=lambda x: int(re.search(r'BANDWIDTH=(\d+)', x[2]).group(1)) if re.search(r'BANDWIDTH=(\d+)', x[2]) else 0, reverse=True)
            video_url=streams_sorted[0][1]
            print(f"[+] Otomatik en yüksek: {video_url}")
        # audio group varsa her iki durumda da topla
        audios=[]
        for m in re.finditer(r'#EXT-X-MEDIA:TYPE=AUDIO.*?NAME="([^"]+)".*?URI="([^"]+)"', master_txt):
            audios.append((m.group(1), urljoin(mbase, m.group(2))))
        if interactive and audios:
            opts=[(f"{n}", (n,u)) for n,u in audios]
            chosen_aud=choose_interactive(opts, "Ses dili seç:", default=1)
            audios=[chosen_aud[1]]
            print(f"[+] Seçilen ses: {chosen_aud[0]}")
        elif audios:
            print(f"[+] audios: {audios}")
    else:
        video_url, audios = parse_master(master_txt, mbase)
        print(f"[+] video: {video_url}")
        print(f"[+] audios: {audios}")
        if interactive and audios:
            opts=[(f"{n}", (n,u)) for n,u in audios]
            chosen_aud=choose_interactive(opts, "Ses dili seç:", default=1)
            audios=[chosen_aud[1]]
            print(f"[+] Seçilen ses: {chosen_aud[0]}")

    if not video_url:
        print("[-] video url yok"); sys.exit(1)

    # altyazı seçimi interaktif
    subs=extract_subtitles(iframe_html)
    chosen_subs=None
    if interactive and subs:
        print(f"\n[+] {len(subs)} altyazı bulundu:")
        for i,(label,url) in enumerate(subs,1):
            print(f"  [{i}] {label} -> {url[:60]}")
        ans=input("Altyazıları indir? [e/H] (e: hepsi, h: hiçbiri, 1,3 gibi seçim): ").strip().lower()
        if ans in ["e","evet","hepsi",""]:
            chosen_subs=subs
        elif ans in ["h","hayir",""]:
            chosen_subs=[]
        else:
            try:
                idxs=[int(x)-1 for x in re.split(r'[,\s]+', ans) if x.strip().isdigit()]
                chosen_subs=[subs[i] for i in idxs if 0<=i<len(subs)]
            except: chosen_subs=[]

    if interactive:
        if not out:
            defslug=urlparse(film_url).path.strip("/").split("/")[-1] or "film"
            defslug=re.sub(r'[\\/:*?"<>|]','_',defslug)[:100]+".mp4"
            ans=input(f"Çıkış dosyası [{defslug}]: ").strip()
            out=ans if ans else defslug
        # limit sor
        if limit is None:
            ans=input("Segment limiti? (boş: hepsi, örnek 20 test için): ").strip()
            if ans.isdigit(): limit=int(ans)

    print("[*] Video playlist çekiliyor...")
    video_txt=fetch(video_url, referer=referer)
    vbase=video_url.rsplit("/",1)[0]+"/"
    v_segs=parse_segments(video_txt, vbase)
    print(f"[+] video segment: {len(v_segs)}")

    a_segs=None
    audio_url=None
    if audios:
        # parse_master'dan gelen audios listesi (seçilmiş)
        audio_url = audios[0][1] if isinstance(audios[0], tuple) else audios[0]
        print("[*] Audio playlist çekiliyor...")
        audio_txt=fetch(audio_url, referer=referer)
        abase=audio_url.rsplit("/",1)[0]+"/"
        a_segs=parse_segments(audio_txt, abase)
        print(f"[+] audio segment: {len(a_segs)}")
    elif streams:
        a_segs=None

    tmpdir="/tmp/hdf_"+re.sub(r'\W+','_',urlparse(film_url).path.strip("/"))[:30]
    # download
    v_list,_ = download_parallel(v_segs, referer, tmpdir, "video", limit=limit)
    if a_segs:
        a_list,_ = download_parallel(a_segs, referer, tmpdir, "audio", limit=limit)

    # ffmpeg ile mux
    print(f"[*] Birlestiriliyor -> {out}")
    if a_segs:
        cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",v_list,"-f","concat","-safe","0","-i",a_list,"-c","copy",out]
    else:
        cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",v_list,"-c","copy",out]
    print(" ".join(cmd))
    ret=subprocess.run(cmd)
    if ret.returncode==0:
        print(f"[+] Bitti: {out} ({os.path.getsize(out)} byte)")
        # altyazı indir
        subs_to_dl = chosen_subs if interactive else subs
        if not interactive:
            subs_to_dl=subs
        if subs_to_dl:
            print(f"[*] {len(subs_to_dl)} altyazı indiriliyor...")
            for label,url in subs_to_dl:
                try:
                    data=fetch(url, referer=iframe)
                    safe_label=re.sub(r'\W+','_',label)[:20]
                    base_out=os.path.splitext(out)[0]
                    sub_path=f"{base_out}.{safe_label}.vtt"
                    open(sub_path,"w",encoding="utf-8").write(data)
                    print(f"  [+] {label}: {sub_path}")
                except Exception as e:
                    print(f"  [!] {label} hata: {e}")
    else:
        print("[-] ffmpeg hata")

if __name__=="__main__":
    main()
