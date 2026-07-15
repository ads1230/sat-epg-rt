import requests
import os
import sys
import re
import html
import json
import urllib.parse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import random

def log(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")
    sys.stdout.flush()

# --- Configuration ---
DAYS = 7
LOGO_DIR = "logos_rt"
CACHE_FILE = "rt_cache.json"
BASE_URL = "https://www.radiotimes.com"

# Radio Times Sky HD Regions
REGIONS = {
    "Anglia": "hnvr", "Cambridgeshire": "hnvs", "Channel Islands": "hnvt",
    "Cumbria": "hnvv", "East Midlands": "hnvw", "Henley on Thames": "hnvx",
    "London": "hnvy", "London (Essex)": "hnvz", "London (Kent)": "hnv2",
    "London (Thames Valley)": "hnv4", "Meridian (East)": "hnv5",
    "Meridian (West)": "hnv6", "North East": "hnv7", "North East Midlands": "hnv8",
    "North West": "hnwb", "North Yorkshire": "hnwc", "Northern Ireland": "hnv9",
    "Northern Oxford": "hnwd", "Republic of Ireland": "hnwf",
    "Scotland (Borders)": "hnwg", "Scotland (Central)": "hnwh",
    "Scotland (North)": "hnwj", "South Lakeland": "hnwk", "Wales": "hnwm",
    "West Dorset": "hnwn", "West England": "hnwp", "West Midlands": "hnwq",
    "Yorkshire": "hnwr", "Yorkshire & Lincolnshire": "hnws"
}

GITHUB_REPO_FULL = os.getenv('GITHUB_REPOSITORY', 'YourUsername/YourRepo')
GITHUB_USER, GITHUB_REPO = GITHUB_REPO_FULL.split('/') if '/' in GITHUB_REPO_FULL else ("Unknown", "Unknown")
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/{LOGO_DIR}/"

UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0'
]

def clean_xml_text(text):
    if not text: return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]', "", str(text))

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
    return {}

def fetch_rt_meta(data_url, session):
    try:
        url = BASE_URL + data_url if data_url.startswith('/') else data_url
        r = session.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            
            # Radio Times data structure extraction
            desc = d.get('description') or d.get('summary', '')
            sub_title = d.get('episodeTitle', '')
            season = d.get('seriesNumber')
            episode = d.get('episodeNumber')
            img = d.get('image', {}).get('url', '')
            
            # Cast & Crew
            cast = d.get('cast', [])
            actors = [c.get('name') for c in cast if c.get('role', '').lower() in ['actor', 'cast']]
            directors = [c.get('name') for c in cast if c.get('role', '').lower() == 'director']
            
            # Categories & Flags
            genres = d.get('genres', [])
            is_film = d.get('type', '').lower() == 'film'
            if is_film and 'Film' not in genres: genres.append('Film')
            
            flags = d.get('flags', [])
            subs = 'Subtitles' in flags or d.get('hasSubtitles')
            ad = 'AudioDescription' in flags or d.get('hasAudioDescription')
            
            return data_url, {
                'desc': desc, 'sub': sub_title, 'sn': season, 'en': episode,
                'img': img, 'ad': ad, 'subs': subs, 'genres': genres,
                'actors': actors, 'directors': directors
            }, 200
        return data_url, {}, r.status_code
    except Exception as e: return data_url, {}, str(e)

def run(target_region=None):
    if not os.path.exists(LOGO_DIR): os.makedirs(LOGO_DIR)
    meta_cache = load_cache()
    now_utc = datetime.now(timezone.utc)
    start_of_today = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)

    items = [(target_region, REGIONS[target_region])] if target_region in REGIONS else REGIONS.items()

    for region_name, nid in items:
        log(f"--- REGION: {region_name} (Radio Times) ---")
        
        session = requests.Session()
        session.headers.update({'User-Agent': random.choice(UAS)})
        
        channels, progs = {}, []
        missing_pids, missing_logos = {}, {}

        # PASS 0: Grab Channel List for Region
        log("   [INFO] Fetching channel list...")
        try:
            r_chans = session.get(f"{BASE_URL}/api/broadcast/broadcast/channels/video?provider=skyhd&region={nid}", timeout=15)
            if r_chans.status_code == 200:
                for chan in r_chans.json():
                    cid = str(chan.get('id'))
                    sched_url = chan.get('scheduleUrl')
                    if not cid or not sched_url: continue
                    
                    channels[cid] = {
                        'name': chan.get('title', 'Unknown'), 
                        'lcn': str(chan.get('channelNumber', '')),
                        'sched_url': sched_url
                    }
                    
                    logo_url = chan.get('logo', {}).get('url')
                    if logo_url:
                        logo_path = os.path.join(LOGO_DIR, f"{cid}.png")
                        if not os.path.exists(logo_path):
                            missing_logos[cid] = (logo_path, logo_url)
        except Exception as e:
            log(f"   [CRITICAL] Failed to fetch channel list: {e}")
            continue

        # PASS 1: Build Schedule (PARALLEL FETCHING)
        schedule_tasks = []
        for day in range(DAYS):
            from_dt = start_of_today + timedelta(days=day)
            to_dt = from_dt + timedelta(days=1)
            
            from_str = urllib.parse.quote(from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            to_str = urllib.parse.quote(to_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            
            for cid, info in channels.items():
                url = f"{BASE_URL}{info['sched_url']}?from={from_str}&to={to_str}"
                schedule_tasks.append((url, cid, from_dt.strftime("%Y-%m-%d")))

        total_schedules = len(schedule_tasks)
        log(f"   [INFO] Fetching {total_schedules} daily schedules across all channels...")
        completed_schedules = 0

        # Highly aggressive threading to rip through the thousands of URLs
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_url = {executor.submit(session.get, url, timeout=15): (url, cid, d_str) for url, cid, d_str in schedule_tasks}
            for future in concurrent.futures.as_completed(future_to_url):
                url, cid, d_str = future_to_url[future]
                completed_schedules += 1
                
                try:
                    r = future.result()
                    if r.status_code == 200:
                        for ev in r.json():
                            data_url = ev.get('dataUrl')
                            start_str = ev.get('start')
                            end_str = ev.get('end')
                            
                            if not data_url or not start_str or not end_str: continue
                            
                            try:
                                start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                end_dt = datetime.strptime(end_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                
                                s_time = start_dt.strftime('%Y%m%d%H%M%S +0000')
                                e_time = end_dt.strftime('%Y%m%d%H%M%S +0000')
                                
                                if data_url not in meta_cache:
                                    missing_pids[data_url] = data_url
                                    
                                progs.append({
                                    'cid': cid, 'pid': data_url, 't': ev.get('title', 'Unknown'),
                                    's': s_time, 'e': e_time
                                })
                            except Exception: pass
                except Exception: pass

                update_iv = max(1, total_schedules // 20)
                if completed_schedules % update_iv == 0 or completed_schedules == total_schedules:
                    pct = completed_schedules / total_schedules
                    bar_len = 20
                    filled = int(bar_len * pct)
                    bar = '█' * filled + '-' * (bar_len - filled)
                    sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Schedule Progress: [{bar}] {pct*100:.1f}% ({completed_schedules}/{total_schedules})")
                    sys.stdout.flush()
        print()

        # PASS 1.5: Download Logos
        total_logos = len(missing_logos)
        if total_logos > 0:
            log(f"   [INFO] Found {total_logos} missing channel logos. Downloading...")
            completed = 0
            for cid, (path, url) in missing_logos.items():
                try:
                    img_data = session.get(url, timeout=10).content
                    with open(path, 'wb') as handler: handler.write(img_data)
                except Exception: pass
                
                completed += 1
                update_iv = max(1, total_logos // 10)
                if completed % update_iv == 0 or completed == total_logos:
                    pct = completed / total_logos
                    bar_len = 20
                    filled = int(bar_len * pct)
                    bar = '█' * filled + '-' * (bar_len - filled)
                    sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Logo Progress: [{bar}] {pct*100:.1f}% ({completed}/{total_logos})")
                    sys.stdout.flush()
            print()

        # PASS 2: Metadata
        total_missing_list = list(missing_pids.items())
        total_to_fetch = len(total_missing_list)
        
        if total_to_fetch > 0:
            log(f"FETCHING {total_to_fetch} metadata items...")
            completed, success_count, blocked_count = 0, 0, 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(fetch_rt_meta, pid, session) for pid, _ in total_missing_list]
                for f in concurrent.futures.as_completed(futures):
                    pid, m_data, status = f.result()
                    completed += 1
                    
                    if status == 200:
                        meta_cache[pid] = m_data
                        success_count += 1
                    else:
                        meta_cache[pid] = {}
                        if status in [403, 429]: blocked_count += 1

                    update_iv = max(1, total_to_fetch // 20)
                    if completed % update_iv == 0 or completed == total_to_fetch:
                        pct = completed / total_to_fetch
                        bar_len = 20
                        filled = int(bar_len * pct)
                        bar = '█' * filled + '-' * (bar_len - filled)
                        sys.stdout.write(f"\r[{datetime.now().strftime('%H:%M:%S')}]    Progress: [{bar}] {pct*100:.1f}% ({completed}/{total_to_fetch}) | Success: {success_count} | Blocks: {blocked_count}")
                        sys.stdout.flush()
                    
                    if blocked_count >= 15:
                        log("\n   [WARNING] Too many blocks detected. Halting metadata fetch to protect API access.")
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
            print()

            # --- SMART CACHE PRUNING (90MB TARGET) ---
            MAX_BYTES = 90 * 1024 * 1024 
            while True:
                cache_str = json.dumps(meta_cache, separators=(',', ':'))
                cache_size = len(cache_str.encode('utf-8'))
                if cache_size <= MAX_BYTES: break
                items_to_remove = max(1000, len(meta_cache) // 20)
                meta_cache = dict(list(meta_cache.items())[items_to_remove:])
                log(f"   [CACHE WARNING] Size hit {cache_size / (1024*1024):.1f}MB. Pruned oldest {items_to_remove} items.")

            with open(CACHE_FILE, 'w', encoding='utf-8') as f: f.write(cache_str)

        # PASS 3: Generate XML
        output_file = f"rt_{region_name.lower().replace(' ', '_').replace('(', '').replace(')', '')}.xml"
        log(f"Writing {output_file}...")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?><tv>\n')
            for cid, info in channels.items():
                f.write(f'  <channel id="{cid}">\n')
                f.write(f'    <display-name>{html.escape(info["name"])}</display-name>\n')
                if info.get('lcn') and info['lcn'] != 'None': 
                    f.write(f'    <lcn>{info["lcn"]}</lcn>\n')
                if os.path.exists(os.path.join(LOGO_DIR, f"{cid}.png")):
                    f.write(f'    <icon src="{GITHUB_RAW_BASE}{cid}.png" />\n')
                f.write(f'  </channel>\n')
                
            for p in progs:
                m = meta_cache.get(p['pid'], {})
                f.write(f'  <programme start="{p["s"]}" stop="{p["e"]}" channel="{p["cid"]}">\n')
                f.write(f'    <title>{html.escape(clean_xml_text(p["t"]))}</title>\n')
                if m.get('sub'): f.write(f'    <sub-title>{html.escape(clean_xml_text(m["sub"]))}</sub-title>\n')
                
                desc = clean_xml_text(m.get('desc', ''))
                if m.get('ad'): desc = f"[AD] {desc}" if desc else "[AD]"
                if desc: f.write(f'    <desc>{html.escape(desc)}</desc>\n')
                
                if m.get('actors') or m.get('directors'):
                    f.write('    <credits>\n')
                    for d in m.get('directors', []): f.write(f'      <director>{html.escape(clean_xml_text(d))}</director>\n')
                    for a in m.get('actors', []): f.write(f'      <actor>{html.escape(clean_xml_text(a))}</actor>\n')
                    f.write('    </credits>\n')
                
                for cat in m.get('genres', []):
                    f.write(f'    <category>{html.escape(clean_xml_text(cat))}</category>\n')

                if m.get('img'): f.write(f'    <icon src="{html.escape(m["img"])}" />\n')
                
                if m.get('sn') and m.get('en'):
                    f.write(f'    <episode-num system="onscreen">S{m["sn"]} E{m["en"]}</episode-num>\n')
                
                if m.get('subs'): f.write('    <subtitles type="onscreen" />\n')
                f.write('  </programme>\n')
            f.write('</tv>')

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
