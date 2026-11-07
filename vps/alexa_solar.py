#!/usr/bin/env python3
"""
Alexa Solar Flask Backend V3
- Uses new /stopgen endpoint for robust stop sequence
- No more timing guesswork - ESP8266 handles entire stop→auto sequence
- stop_and_auto() is now a simple single call with longer timeout
"""
from datetime import datetime, timedelta, timezone
import math
import re
from flask import Flask, request, jsonify
import requests
import threading
import time

try:
    from zoneinfo import ZoneInfo
    SITE_TZ = ZoneInfo("America/Tijuana")
except ImportError:  # the VPS is on Python 3.8
    import pytz
    SITE_TZ = pytz.timezone("America/Tijuana")

SITE_LAT = 32.36
SITE_LON = -117.06

app = Flask(__name__)

ESP8266_DATA_URL = "http://10.8.0.2:8080/data"
ESP8266_GEN_URL = "http://10.8.0.2:8080/setgen"
ESP8266_STOPGEN_URL = "http://10.8.0.2:8080/stopgen"  # NEW V1.4.29 endpoint
ESP8266_EVENTS_URL = "http://10.8.0.2:8080/events"
ESP8266_AUTOGEN_URL = "http://10.8.0.2:8080/autogen"
ESP8266_CONFIG_URL = "http://10.8.0.2:8080/config"

# Solar agent on the KAMRUI, reached over WireGuard through the Pi5.
AGENT_ASK_URL = "http://192.168.3.152:8090/ask"
# The agent answers within AGENT_WAIT_S or says it will use Telegram; Alexa gives
# up after about 8 s, and Gemma takes 15 to 30.
AGENT_WAIT_S = 5
AGENT_TIMEOUT = 7
# After the agent, a screen still has to be built inside Alexa's 8 s. Past this many
# seconds it goes without the Pi5's figures rather than risk the answer.
AGENT_STATUS_DEADLINE = 6.5
AGENT_STATUS_TIMEOUT = (0.5, 0.5)

AGENT_SENT_TO_PHONE = {
    "en": "Answer sent to your phone",
    "es": "Respuesta enviada a tu teléfono",
}
SCREEN_QUESTION_CHARS = 300
SCREEN_ANSWER_CHARS = 2000

AGENT_FALLBACK = {
    "en": "The solar agent is not answering right now.",
    "es": "El agente solar no responde en este momento.",
}

AGENT_LATER = {
    "en": "I'm checking — I'll send the answer to your phone.",
    "es": "Lo estoy revisando, te envío la respuesta al teléfono.",
}


# Alexa drops the carrier word from a SearchQuery slot: with `why {query}`, "why did
# the Kubota start" arrives as "did the Kubota start". So there is one intent per
# leading question word, and this puts the word back.
AGENT_PREFIX = {
    "AskAgentIntent": "",
    # English
    "AskWhyIntent": "why",
    "AskHowIntent": "how",
    "AskWhatIntent": "what",
    "AskWhatIsIntent": "what is",
    "AskWhenIntent": "when",
    "AskWhichIntent": "which",
    "AskWillIntent": "will",
    "AskIsIntent": "is",
    "AskAreIntent": "are",
    "AskDidIntent": "did",
    "AskDoIntent": "do",
    "AskDoesIntent": "does",
    "AskWasIntent": "was",
    "AskShouldIntent": "should",
    # Spanish
    "AskPorQueIntent": "por qué",
    "AskComoIntent": "cómo",
    "AskCuantoIntent": "cuánto",
    "AskCuantaIntent": "cuánta",
    "AskCuantosIntent": "cuántos",
    "AskQueIntent": "qué",
    "AskCualIntent": "cuál",
    "AskCuandoIntent": "cuándo",
    "AskHayIntent": "hay",
    "AskVaIntent": "va a",
    "AskEstaIntent": "está",
}


def ask_agent(query, lang):
    """Put a free-form question to the solar agent. Returns speech text."""
    try:
        resp = requests.post(AGENT_ASK_URL,
                             json={"text": query, "lang": lang, "deliver": "telegram",
                                   "wait_s": AGENT_WAIT_S},
                             timeout=AGENT_TIMEOUT)
        if resp.status_code == 202:
            return AGENT_LATER[lang]
        if resp.status_code != 200:
            print(f"ERROR ask_agent: HTTP {resp.status_code}")
            return AGENT_FALLBACK[lang]
        reply = (resp.text or "").strip()
        return reply or AGENT_FALLBACK[lang]
    except Exception as e:
        print(f"ERROR ask_agent: {e}")
        return AGENT_FALLBACK[lang]

def get_solar_data(timeout=5):
    try:
        resp = requests.get(ESP8266_DATA_URL, timeout=timeout)
        return resp.json()
    except Exception as e:
        print(f"ERROR get_solar_data: {e}")
        return None

def get_thresholds(timeout=2):
    """{generator: (start V, stop V)} from the Pi5's /config, or None.

    A GET with no parameters writes nothing. The response also holds the Telegram
    token, so only the two voltages are taken and the response is never logged.
    """
    try:
        cfg = requests.get(ESP8266_CONFIG_URL, timeout=timeout).json()['config']
        return {gen: (float(cfg[gen]['startVoltage']), float(cfg[gen]['stopVoltage']))
                for gen in ('mep803a', 'kubota')}
    except Exception as e:
        print(f"ERROR get_thresholds: {type(e).__name__}")
        return None

def get_events():
    try:
        resp = requests.get(ESP8266_EVENTS_URL, timeout=5)
        return resp.json()
    except Exception as e:
        print(f"ERROR get_events: {e}")
        return None

def set_autogen(enable):
    """Set auto generator control - returns (success, actual_state)"""
    try:
        url = f"{ESP8266_AUTOGEN_URL}?enable={'1' if enable else '0'}"
        print(f"DEBUG set_autogen: Calling {url}")
        resp = requests.get(url, timeout=5)
        print(f"DEBUG set_autogen: Response status={resp.status_code}, text={resp.text}")
        
        if resp.status_code != 200:
            return False, None
        
        time.sleep(0.5)
        verify_resp = requests.get(ESP8266_AUTOGEN_URL, timeout=5)
        actual_state = verify_resp.text.strip()
        print(f"DEBUG set_autogen: Verify response={actual_state}")
        
        expected = "ON" if enable else "OFF"
        success = (actual_state == expected)
        
        if not success:
            print(f"WARNING set_autogen: Expected {expected} but got {actual_state}")
        
        return success, actual_state
        
    except Exception as e:
        print(f"ERROR set_autogen: {e}")
        return False, None

def get_autogen_status():
    """Get current auto generator control status"""
    try:
        resp = requests.get(ESP8266_AUTOGEN_URL, timeout=5)
        return resp.text.strip()
    except Exception as e:
        print(f"ERROR get_autogen_status: {e}")
        return None

def set_generator(gen_id, state):
    """Set generator mode (0=OFF, 1=ON, 2=AUTO) - runs in background thread"""
    def do_command():
        try:
            url = f"{ESP8266_GEN_URL}?id={gen_id}&state={state}"
            print(f"DEBUG set_generator: Calling {url}")
            resp = requests.get(url, timeout=60)
            print(f"DEBUG set_generator: Response={resp.text}")
        except Exception as e:
            print(f"ERROR set_generator: {e}")
    thread = threading.Thread(target=do_command)
    thread.start()
    return True

def stop_and_auto(gen_id):
    """
    V3: Uses new /stopgen endpoint which handles EVERYTHING:
    - Ramp down chargers (15s steps)
    - Hold at 0% for 2 minutes
    - Stop generator
    - Re-enable chargers
    - Set generator to AUTO mode
    
    Total time: ~3-4 minutes (ESP8266 handles it all)
    """
    def do_command():
        try:
            print(f"DEBUG stop_and_auto: Calling /stopgen for gen_id={gen_id}")
            # Use the new /stopgen endpoint - it handles everything internally
            # Timeout is 300 seconds (5 min) to allow for full sequence
            url = f"{ESP8266_STOPGEN_URL}?id={gen_id}"
            resp = requests.get(url, timeout=300)
            print(f"DEBUG stop_and_auto: Response={resp.text}")
            print(f"DEBUG stop_and_auto: Complete for gen_id={gen_id}")
        except Exception as e:
            print(f"ERROR stop_and_auto: {e}")
    thread = threading.Thread(target=do_command)
    thread.start()
    return True

def get_gen_mode(mode, spanish=False):
    if mode == 0:
        return "apagado" if spanish else "OFF"
    elif mode == 1:
        return "prendido" if spanish else "RUNNING"
    elif mode == 2:
        return "en automatico" if spanish else "AUTO"
    return "desconocido" if spanish else "UNKNOWN"

def get_battery_color(soc):
    if soc >= 70:
        return "#34D399"
    elif soc >= 40:
        return "#FBBF24"
    else:
        return "#F87171"

def get_gen_id(generator_name):
    # Whole words only: "small" and "install" must not count as "all".
    name = generator_name.lower()
    if re.search(r'\bkubota\b', name):
        return 50
    elif re.search(r'\b(mep|military|militar)\b', name):
        return 51
    elif re.search(r'\b(all|both|todos|ambos|los dos)\b', name):
        return 'all'
    return None

def resolved_generator(data):
    """The generator the slot names, or None. Never a placeholder like "unknown".

    Alexa's entity resolution gives the slot type's canonical name, but only on
    ER_SUCCESS_MATCH. Otherwise the raw value is what's left. A name that maps to
    no generator counts as no generator, so it can't reach speech or a command.
    """
    intent = data.get('request', {}).get('intent') or {}
    slot = (intent.get('slots') or {}).get('generator') or {}
    candidates = [slot.get('value')]
    try:
        authority = slot['resolutions']['resolutionsPerAuthority'][0]
        if authority['status']['code'] == 'ER_SUCCESS_MATCH':
            candidates.insert(0, authority['values'][0]['value']['name'])
    except (KeyError, IndexError, TypeError):
        pass
    for name in candidates:
        if isinstance(name, str) and get_gen_id(name):
            return name
    return None

def spoken_generator(name):
    """The name for speech only: the sentences already say "the" or "el"."""
    return re.sub(r'^\s*(the|el)\s+', '', name, flags=re.IGNORECASE)

def ask_which_generator(spanish):
    speech = "¿Cuál generador, el Kubota o el MEP?" if spanish else "Which generator, the Kubota or the MEP?"
    return build_response(speech, end_session=False)

def is_spanish(data):
    locale = data.get('request', {}).get('locale', 'en-US')
    return locale.startswith('es')

def supports_apl(data):
    try:
        supported = data.get('context', {}).get('System', {}).get('device', {}).get('supportedInterfaces', {})
        return 'Alexa.Presentation.APL' in supported
    except:
        return False

def threshold_line(thresholds, gen, spanish):
    if not thresholds:
        return ""
    start, stop = thresholds[gen]
    if spanish:
        return f"Prende {start:.1f} V · Apaga {stop:.1f} V"
    return f"Start {start:.1f} V · Stop {stop:.1f} V"

EMOJI = re.compile("[\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿️‍]")

def no_emoji(text):
    """Screens carry no emoji, and Pi5 events and agent replies can contain them."""
    return re.sub(r"\s{2,}", " ", EMOJI.sub("", text)).strip()

def clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

# --- the sky ---------------------------------------------------------------------------

def current_time():
    return time.time()

def sun_times(day):
    """Sunrise and sunset for a calendar date, as epoch seconds. NOAA's general formula."""
    gamma = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat = math.radians(SITE_LAT)
    hour_angle = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl))
                                        - math.tan(lat) * math.tan(decl)))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return (midnight + 60 * (720 - 4 * (SITE_LON + hour_angle) - eqtime),
            midnight + 60 * (720 - 4 * (SITE_LON - hour_angle) - eqtime))

def sky_state(now):
    """Day runs from the exact sunrise to the exact sunset, both included."""
    today = datetime.fromtimestamp(now, SITE_TZ).date()
    sunrise, sunset = sun_times(today)
    if sunrise <= now <= sunset:
        return {"day": True, "fraction": (now - sunrise) / (sunset - sunrise), "next": sunset}
    following = sunrise if now < sunrise else sun_times(today + timedelta(days=1))[0]
    return {"day": False, "fraction": None, "next": following}

def clock_text(ts, spanish):
    t = datetime.fromtimestamp(ts, SITE_TZ)
    hour = t.hour % 12 or 12
    if spanish:
        return f"{hour}:{t.minute:02d} {'p. m.' if t.hour >= 12 else 'a. m.'}"
    return f"{hour}:{t.minute:02d} {'pm' if t.hour >= 12 else 'am'}"

def sun_xy(layout, fraction):
    """Left horizon at 0, the top at 0.5, right horizon at 1, in the layout's own units."""
    angle = math.pi * fraction
    return (round(layout["arc_cx"] - layout["arc_r"] * math.cos(angle), 1),
            round(layout["horizon"] - layout["arc_r"] * math.sin(angle), 1))

PALETTE = {
    True: {"sky": "#13306B", "ground": "#081734", "horizon": "#2B4A8A", "secondary": "#AFC3E8", "dim": "#7F95BF"},
    False: {"sky": "#070B1A", "ground": "#04060D", "horizon": "#1C2440", "secondary": "#8B95A5", "dim": "#5B6575"},
}
PRIMARY = "#FFFFFF"
AMBER = "#FBBF24"
BLUE = "#60A5FA"
GREEN = "#34D399"

GEN_STATES = {
    "running": (GREEN, "Running", "Prendido"),
    "auto": (AMBER, "Auto", "Automático"),
    "off": ("#5B6575", "Off", "Apagado"),
    "fault": ("#F87171", "Fault", "Falla"),
}
AGS_RUNNING = 9
AGS_TURNING = (0, 1, 2, 3, 4, 5, 6, 7, 8, 11)  # starting or stopping, as the dashboard's chips

def generator_state(solar, gen):
    """off, auto, running or fault. The same order as the Pi5 dashboard's chipFor()."""
    short, long = ("mep", "mep803a") if gen == "mep" else ("kubota", "kubota")
    if solar.get(f"{short}State") == "ags_fault" or solar.get(f"{short}Fault"):
        return "fault"
    mode = solar.get(f"{long}Mode", 0)
    action = solar.get(f"{long}Action", 255)
    if action == AGS_RUNNING or action in AGS_TURNING or mode in (1, 3):
        return "running"
    return "auto" if mode == 2 else "off"

def generator_turning(solar, gen):
    long = "mep803a" if gen == "mep" else "kubota"
    return solar.get(f"{long}Action", 255) == AGS_RUNNING

def generator_charge_w(solar, gen):
    """MEP charges through the two XW Pros, the Kubota through the XW+."""
    if gen == "mep":
        return (solar.get('chargePower1') or 0) + (solar.get('chargePower2') or 0)
    return solar.get('chargePower3') or 0

def kw_text(watts):
    return f"{watts / 1000.0:.1f} kW"

def flow_divisor(watts):
    """elapsedTime is divided by this: the more power, the faster the dashes move."""
    return round(max(10.0, min(60.0, 30000.0 / max(watts, 1))), 1)

# --- the two scenes, in their own units --------------------------------------------------

SHOW = {
    "key": "show", "w": 960, "h": 600, "horizon": 372, "arc_cx": 480, "arc_r": 288,
    "battery": (360, 342, 72, 30), "house": (506, 412, 46),
    "house_from": (436, 360), "house_ctrl": (500, 372), "house_to": (514, 420),
    "gen_from": {"mep": (352, 402), "kubota": (352, 464)},
    "gen_to": {"mep": (372, 372), "kubota": (384, 372)},
    "gen_ctrl": {"mep": (372, 402), "kubota": (384, 440)},
    "gen_dot": {"mep": (52, 402), "kubota": (52, 464)},
    "gen_label": {"mep": (394, 382), "kubota": (356, 470)},
    # The sun's flow bends out to here, so it doesn't cross the big numbers or the icon.
    "hero": (300, 500),
    "label_box": (70, 23), "label_slot": 115.2,
    "keep_out": [(0, 12, 300, 50), (672, 12, 960, 50), (335, 231, 456, 372)],
    "moon": (112, 118), "moon_r": 20,
    "stars": [(200, 70), (330, 150), (455, 60), (610, 130), (720, 62), (820, 170), (905, 96)],
    "stroke": 3.5, "dash": [2, 7], "dot_r": 7, "sun_r": 13, "glow_r": 24,
}
SPOT = {
    "key": "spot", "w": 480, "h": 480, "horizon": 278, "arc_cx": 240, "arc_r": 160,
    "battery": (198, 243, 84, 31), "house": (366, 298, 36),
    "house_from": (288, 258), "house_ctrl": (350, 268), "house_to": (376, 304),
    "gen_from": {"mep": (184, 310), "kubota": (184, 338)},
    "gen_to": {"mep": (208, 274), "kubota": (202, 274)},
    "gen_ctrl": {"mep": (186, 284), "kubota": (172, 292)},
    "gen_dot": {"mep": (78, 310), "kubota": (78, 338)},
    "gen_label": {"mep": (140, 303), "kubota": (140, 331)},
    "hero": (186, 296),
    # What the solar label has to stay out of, as (left, top, right, bottom).
    "label_box": (56, 18), "label_slot": 105.6, "keep_out": [(140, 22, 340, 58), (194, 239, 290, 278)],
    "moon": (140, 112), "moon_r": 16,
    "stars": [(95, 170), (205, 62), (250, 130), (300, 72), (345, 140), (400, 185), (170, 205)],
    "stroke": 3, "dash": [2, 6], "dot_r": 5, "sun_r": 10, "glow_r": 18,
}
SCENE_PARAMS = {
    "color": ["sky", "ground", "horizon", "battColor", "mepDot", "kubDot"],
    "number": ["sunX", "sunY", "dayOpacity", "nightOpacity", "solarOpacity", "houseOpacity",
               "mepOpacity", "kubOpacity", "solarDash", "houseDash", "mepDash", "kubDash"],
    "string": ["solarPath", "mepPath", "kubPath", "fillPath"],
}

def circle(x, y, r):
    return f"M{x - r} {y}a{r} {r} 0 1 0 {2 * r} 0a{r} {r} 0 1 0 {-2 * r} 0z"

def quad(start, ctrl, end):
    return f"M{start[0]} {start[1]}Q{ctrl[0]} {ctrl[1]} {end[0]} {end[1]}"

def battery_fill_width(soc, inner_width):
    return round(inner_width * max(0, min(100, soc)) / 100.0, 1)

def battery_fill_path(layout, soc):
    x, y, w, h = layout["battery"]
    return f"M{x + 3} {y + 3}h{battery_fill_width(soc, w - 6)}v{h - 6}h{-battery_fill_width(soc, w - 6)}z"

def solar_curve(layout, sun):
    """A slight curve from the sun into the near side of the battery, under the big numbers."""
    x, y, w, h = layout["battery"]
    end = (x + w + 5, y + h / 2) if sun[0] >= x + w / 2 else (x - 1, y + h / 2)
    ctrl_x = max(sun[0], layout["hero"][1]) if end[0] > x else min(sun[0], layout["hero"][0])
    return (sun, (ctrl_x, end[1]), end)

def curve_points(start, ctrl, end, count=120):
    points = []
    for i in range(count + 1):
        t = i / float(count)
        points.append(((1 - t) ** 2 * start[0] + 2 * t * (1 - t) * ctrl[0] + t ** 2 * end[0],
                       (1 - t) ** 2 * start[1] + 2 * t * (1 - t) * ctrl[1] + t ** 2 * end[1]))
    return points

def box_distance(box, point):
    left, top, right, bottom = box
    return math.hypot(max(left - point[0], 0, point[0] - right), max(top - point[1], 0, point[1] - bottom))

def boxes_touch(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

LABEL_LINE_CLEARANCE = 8
LABEL_GLOW_CLEARANCE = 16
LABEL_ARC_CLEARANCE = 4
LABEL_EDGE_CLEARANCE = 8
# Nearest first, sideways or further up, never below the first try.
LABEL_OFFSETS = sorted(((dx, -dy) for dx in range(-160, 161, 4) for dy in range(0, 161, 4)),
                       key=lambda o: o[0] ** 2 + o[1] ** 2)

def label_fits(layout, box, sun, line, arc):
    if box[3] > layout["horizon"] - 4 or box[1] < 0 or box[0] < 0 or box[2] > layout["w"]:
        return False
    if layout["key"] == "spot":
        centre, rim = layout["w"] / 2.0, layout["w"] / 2.0 - LABEL_EDGE_CLEARANCE
        if any(math.hypot(x - centre, y - centre) > rim for x in box[::2] for y in box[1::2]):
            return False
    if any(boxes_touch(box, zone) for zone in layout["keep_out"]):
        return False
    if box_distance(box, sun) < layout["glow_r"] + LABEL_GLOW_CLEARANCE:
        return False
    if any(box_distance(box, p) < LABEL_LINE_CLEARANCE for p in line):
        return False
    return all(box_distance(box, p) >= LABEL_ARC_CLEARANCE for p in arc)

def solar_label_box(layout, sun):
    """The nearest place above the flow's midpoint that's clear of the line, glow and text."""
    start, ctrl, end = solar_curve(layout, sun)
    line = curve_points(start, ctrl, end)
    mid = curve_points(start, ctrl, end, 2)[1]
    w, h = layout["label_box"]
    cx, r, horizon = layout["arc_cx"], layout["arc_r"], layout["horizon"]
    arc = [(cx - r * math.cos(math.pi * i / 60.0), horizon - r * math.sin(math.pi * i / 60.0)) for i in range(61)]
    for avoiding_arc in (arc, []):
        for dx, dy in LABEL_OFFSETS:
            bottom = mid[1] - LABEL_LINE_CLEARANCE + dy
            box = (mid[0] + dx - w / 2.0, bottom - h, mid[0] + dx + w / 2.0, bottom)
            if label_fits(layout, box, sun, line, avoiding_arc):
                return box
    bottom = mid[1] - LABEL_LINE_CLEARANCE
    return (mid[0] - w / 2.0, bottom - h, mid[0] + w / 2.0, bottom)

def flow_path(colour, opacity, path, dash, layout):
    return {"type": "path", "pathData": path, "stroke": colour, "strokeOpacity": opacity,
            "strokeWidth": layout["stroke"], "strokeDashArray": layout["dash"],
            "strokeDashOffset": dash, "strokeLineCap": "round"}

def scene_graphic(layout):
    x, y, w, h = layout["battery"]
    hx, hy, hs = layout["house"]
    moon_x, moon_y = layout["moon"]
    r = layout["moon_r"]
    left = layout["arc_cx"] - layout["arc_r"]
    right = layout["arc_cx"] + layout["arc_r"]
    horizon = layout["horizon"]
    full_w, full_h = layout["w"], layout["h"]
    return {
        "type": "AVG", "version": "1.2", "width": full_w, "height": full_h,
        "parameters": [{"name": n, "type": t} for t, names in SCENE_PARAMS.items() for n in names],
        "items": [
            {"type": "path", "fill": "${sky}", "pathData": f"M0 0H{full_w}V{full_h}H0z"},
            {"type": "group", "opacity": "${nightOpacity}", "items": [
                {"type": "path", "fill": "#CBD5E1",
                 "pathData": "".join(circle(sx, sy, 1.5) for sx, sy in layout["stars"])},
                {"type": "path", "fill": "#E5E7EB", "pathData": circle(moon_x, moon_y, r)},
                {"type": "path", "fill": "${sky}", "pathData": circle(moon_x + r * 0.45, moon_y - r * 0.2, r * 0.85)},
            ]},
            {"type": "path", "stroke": AMBER, "strokeOpacity": "${dayOpacity * 0.5}", "strokeWidth": 1.5,
             "strokeDashArray": [4, 8],
             "pathData": f"M{left} {horizon}A{layout['arc_r']} {layout['arc_r']} 0 0 1 {right} {horizon}"},
            flow_path(AMBER, "${solarOpacity}", "${solarPath}", "${solarDash}", layout),
            {"type": "path", "fill": "${ground}", "pathData": f"M0 {horizon}H{full_w}V{full_h}H0z"},
            {"type": "path", "stroke": "${horizon}", "strokeWidth": 1.5, "pathData": f"M0 {horizon}H{full_w}"},
            {"type": "group", "opacity": "${dayOpacity}", "translateX": "${sunX}", "translateY": "${sunY}", "items": [
                {"type": "path", "fill": AMBER, "fillOpacity": 0.25, "pathData": circle(0, 0, layout["glow_r"])},
                {"type": "path", "fill": AMBER, "pathData": circle(0, 0, layout["sun_r"])},
            ]},
            flow_path(BLUE, "${houseOpacity}", quad(layout["house_from"], layout["house_ctrl"], layout["house_to"]),
                      "${houseDash}", layout),
            flow_path(GREEN, "${mepOpacity}", "${mepPath}", "${mepDash}", layout),
            flow_path(GREEN, "${kubOpacity}", "${kubPath}", "${kubDash}", layout),
            {"type": "path", "fill": "${battColor}", "pathData": "${fillPath}"},
            {"type": "path", "stroke": "#E3E8EF", "strokeWidth": 2, "pathData": f"M{x} {y}h{w}v{h}h{-w}z"},
            {"type": "path", "fill": "#E3E8EF", "pathData": f"M{x + w} {y + h * 0.3}h4v{h * 0.4}h-4z"},
            {"type": "path", "stroke": BLUE, "strokeWidth": 2, "strokeLineCap": "round",
             "pathData": (f"M{hx} {hy + hs * 0.45}L{hx + hs * 0.5} {hy}L{hx + hs} {hy + hs * 0.45}"
                          f"M{hx + hs * 0.15} {hy + hs * 0.35}V{hy + hs}H{hx + hs * 0.85}V{hy + hs * 0.35}")},
            {"type": "path", "fill": "${mepDot}",
             "pathData": circle(*layout["gen_dot"]["mep"], layout["dot_r"])},
            {"type": "path", "fill": "${kubDot}",
             "pathData": circle(*layout["gen_dot"]["kubota"], layout["dot_r"])},
        ],
    }

def scene_component(layout):
    key, period = layout["key"], sum(layout["dash"])
    dash = lambda name: f"${{-(elapsedTime / solar.{name}Div) % {period}}}"
    return {
        "type": "VectorGraphic", "source": f"{key}Scene", "scale": "fill",
        "position": "absolute", "width": "100vw", "height": "100vh",
        "sky": "${solar.sky}", "ground": "${solar.ground}", "horizon": "${solar.horizon}",
        "battColor": "${solar.batteryColor}", "mepDot": "${solar.mepDot}", "kubDot": "${solar.kubotaDot}",
        "dayOpacity": "${solar.dayOpacity}", "nightOpacity": "${solar.nightOpacity}",
        "solarOpacity": "${solar.solarOpacity}", "houseOpacity": "${solar.houseOpacity}",
        "mepOpacity": "${solar.mepOpacity}", "kubOpacity": "${solar.kubotaOpacity}",
        "solarDash": dash("solar"), "houseDash": dash("house"), "mepDash": dash("mep"), "kubDash": dash("kubota"),
        "sunX": f"${{solar.{key}SunX}}", "sunY": f"${{solar.{key}SunY}}",
        "solarPath": f"${{solar.{key}SolarPath}}", "fillPath": f"${{solar.{key}FillPath}}",
        "mepPath": f"${{solar.{key}MepPath}}", "kubPath": f"${{solar.{key}KubotaPath}}",
    }

def scene_text(text, left, top, width, size, color="#FFFFFF", align="left", **extra):
    return {"type": "Text", "position": "absolute", "text": text, "left": left, "top": top,
            "width": width, "fontSize": size, "color": color, "textAlign": align, **extra}

def gen_rows_show():
    rows = []
    for gen, name, top, under in (("mep", "MEP-803A", "64.8vh", "68.8vh"),
                                  ("kubota", "Kubota", "75.2vh", "79.2vh")):
        rows += [
            scene_text(name, "7.2vw", top, "20vw", "3.3vh"),
            scene_text(f"${{solar.{gen}Word}}", "20vw", top, "15.5vw", "3vh", f"${{solar.{gen}Dot}}", "right"),
            scene_text(f"${{solar.{gen}Thresholds}}", "7.2vw", under, "29vw", "2.5vh", "${solar.secondary}",
                       when=f"${{solar.{gen}Thresholds != ''}}"),
        ]
    return rows

def flow_labels(layout):
    key = layout["key"]
    return [
        scene_text(f"${{solar.{gen}Kw}}", f"${{solar.{key}{gen.title()}LabelX}}", f"${{solar.{key}{gen.title()}LabelY}}",
                   "11vw", "2.6vh", GREEN, when=f"${{solar.{gen}Flow}}")
        for gen in ("mep", "kubota")
    ] + [scene_text("${solar.solarKw}", f"${{solar.{key}SolarLabelX}}", f"${{solar.{key}SolarLabelY}}",
                    "12vw" if key == "show" else "22vw", "3vh", AMBER, "center", when="${solar.solarFlow}")]

def show_scene():
    return page({"type": "Container", "width": "100%", "height": "100%", "items": [
        scene_component(SHOW),
        # the top line
        {"type": "VectorGraphic", "source": "dot", "position": "absolute", "left": "3.6vw", "top": "4.6vh",
         "width": "14dp", "height": "14dp", "colour": "${solar.autoDot}"},
        scene_text("${solar.autoText}", "6vw", "3.4vh", "50vw", "3vh", "${solar.secondary}"),
        scene_text("${solar.nowText}", "70vw", "3vh", "26vw", "3.7vh", align="right"),
        # the battery
        scene_text("${solar.batterySOC}%", "25.2vw", "38.6vh", "32vw", "9.33vh", "${solar.batteryColor}",
                   "center", fontWeight="300"),
        scene_text("${solar.batteryVoltage}", "25.2vw", "51vh", "32vw", "3.5vh", "#E3E8EF", "center"),
        # the flows' labels
        *flow_labels(SHOW),
        scene_text("${solar.houseLabel}", "47vw", "77vh", "22vw", "2.6vh", BLUE, "center", when="${solar.houseFlow}"),
        # the ground
        *gen_rows_show(),
        scene_text("${solar.arrayTitle}", "72vw", "64.8vh", "24vw", "2.7vh", "${solar.secondary}"),
        scene_text("MPPT80", "72vw", "70vh", "12vw", "3vh", "#E3E8EF"),
        scene_text("${solar.mpptW}", "84vw", "70vh", "12vw", "3vh", align="right"),
        scene_text("${solar.southLabel}", "72vw", "75vh", "12vw", "3vh", "#E3E8EF"),
        scene_text("${solar.southW}", "84vw", "75vh", "12vw", "3vh", align="right"),
        scene_text("${solar.westLabel}", "72vw", "80vh", "12vw", "3vh", "#E3E8EF"),
        scene_text("${solar.westW}", "84vw", "80vh", "12vw", "3vh", align="right"),
        scene_text("${solar.notice}", "30vw", "86vh", "40vw", "2.7vh", "${solar.secondary}", "center",
                   when="${solar.notice != ''}"),
        scene_text("${solar.skyEvent}", "30vw", "92vh", "40vw", "3vh", "${solar.secondary}", "center"),
    ]}, when=NOT_ROUND)

def spot_scene():
    return page({"type": "Container", "width": "100%", "height": "100%", "items": [
        scene_component(SPOT),
        scene_text("${solar.nowText}", "25vw", "5.5vh", "50vw", "4vh", align="center"),
        *flow_labels(SPOT),
        scene_text("${solar.batterySOC}%", "30vw", "58.6vh", "40vw", "10vh", "${solar.batteryColor}",
                   "center", fontWeight="300"),
        scene_text("${solar.batteryVoltage}", "30vw", "71.5vh", "40vw", "3vh", "#E3E8EF", "center"),
        scene_text("MEP", "18.2vw", "62.8vh", "12vw", "3vh", "#E3E8EF"),
        scene_text("Kubota", "18.2vw", "68.6vh", "12vw", "3vh", "#E3E8EF"),
        scene_text("${solar.houseKw}", "70vw", "71vh", "20vw", "2.8vh", BLUE, "center", when="${solar.houseFlow}"),
        scene_text("${solar.skyEvent}", "20vw", "80.5vh", "60vw", "3vh", "${solar.secondary}", "center"),
        scene_text("${solar.notice}", "20vw", "85.5vh", "60vw", "2.5vh", "${solar.secondary}", "center",
                   when="${solar.notice != ''}"),
    ]}, when=ROUND)

ROUND = "${viewport.shape == 'round'}"
NOT_ROUND = "${viewport.shape != 'round'}"
# 15% each way is the square that fits inside the circle.
PADDING = {
    "paddingLeft": "${viewport.shape == 'round' ? '15vw' : '5vw'}",
    "paddingRight": "${viewport.shape == 'round' ? '15vw' : '5vw'}",
    "paddingTop": "${viewport.shape == 'round' ? '15vh' : '4vh'}",
    "paddingBottom": "${viewport.shape == 'round' ? '15vh' : '4vh'}",
}
TEXT_ALIGN = "${viewport.shape == 'round' ? 'center' : 'left'}"

def apl_screen(items, graphics=None):
    document = {
        "type": "APL",
        "version": "1.6",
        "theme": "dark",
        "settings": {"idleTimeout": 60000},
        "mainTemplate": {"parameters": ["solar"], "items": items},
    }
    if graphics:
        document["graphics"] = graphics
    return document

def page(content, when=None):
    """The whole screen's background. A Frame holds one child, in "item"."""
    frame = {"type": "Frame", "width": "100vw", "height": "100vh",
             "backgroundColor": "${solar.sky}", "item": content}
    if when:
        frame["when"] = when
    return frame

def build_apl_document():
    return apl_screen([spot_scene(), show_scene()], graphics={
        "showScene": scene_graphic(SHOW),
        "spotScene": scene_graphic(SPOT),
        "dot": {"type": "AVG", "version": "1.2", "width": 14, "height": 14,
                "parameters": [{"name": "colour", "type": "color"}],
                "items": [{"type": "path", "fill": "${colour}", "pathData": circle(7, 7, 7)}]},
    })

def build_answer_document():
    return apl_screen([page({
        "type": "Container",
        "width": "100%",
        "height": "100%",
        **PADDING,
        "items": [
            {"type": "Text", "text": "${solar.question}", "color": "${solar.secondary}", "fontSize": "4vh",
             "maxLines": 2, "textAlign": TEXT_ALIGN},
            {"type": "ScrollView", "grow": 1, "shrink": 1, "marginTop": "2vh", "item": {
                "type": "Text", "text": "${solar.answer}", "color": PRIMARY,
                "fontSize": "${viewport.shape == 'round' ? '4vh' : '5vh'}", "textAlign": TEXT_ALIGN}},
            {"type": "Text", "when": "${solar.statusLine != ''}", "text": "${solar.statusLine}",
             "color": "${solar.statusColor}", "fontSize": "3.5vh", "marginTop": "2vh", "textAlign": "center"},
            {"type": "Text", "when": "${solar.genLine != ''}", "text": "${solar.genLine}",
             "color": "${solar.secondary}", "fontSize": "3vh", "textAlign": "center"},
        ]
    })])

def build_answer_datasource(question, answer, solar, spanish, now=None):
    state = sky_state(current_time() if now is None else now)
    screen = {"question": clip(no_emoji(question), SCREEN_QUESTION_CHARS),
              "answer": clip(no_emoji(answer), SCREEN_ANSWER_CHARS),
              "statusLine": "", "statusColor": PRIMARY, "genLine": "", **PALETTE[state["day"]]}
    if solar:
        soc = solar.get('batterySOC', 0)
        voltage = round(solar.get('batteryVoltage', 0), 1)
        total = (solar.get('mppt80PVPower', 0) + solar.get('southArrayPVPower', 0)
                 + solar.get('westArrayPVPower', 0))
        word = lambda gen: GEN_STATES[generator_state(solar, gen)][2 if spanish else 1]
        screen.update(statusLine=f"{soc}% · {voltage} V · {total} W",
                      statusColor=get_battery_color(soc),
                      genLine=f"MEP-803A: {word('mep')} · Kubota: {word('kubota')}")
    return screen

def build_events_document():
    return apl_screen([page({
        "type": "Container",
        "width": "100%",
        "height": "100%",
        **PADDING,
        "items": [
            {"type": "Text", "text": "${solar.title}", "color": PRIMARY, "fontSize": "5vh",
             "fontWeight": "300", "textAlign": "center"},
            {"type": "ScrollView", "grow": 1, "shrink": 1, "marginTop": "2vh", "item": {
                "type": "Container", "width": "100%", "items": [{
                    "type": "Text", "data": "${solar.events}", "text": "${data}", "color": "#E3E8EF",
                    "fontSize": "${viewport.shape == 'round' ? '3.5vh' : '4vh'}",
                    "paddingBottom": "2vh", "textAlign": TEXT_ALIGN}]}},
        ]
    })])

def build_events_datasource(events, spanish, now=None):
    state = sky_state(current_time() if now is None else now)
    return {"title": "Eventos recientes" if spanish else "Recent events",
            "events": [no_emoji(e) for e in events], **PALETTE[state["day"]]}

def scene_geometry(layout, state, soc):
    """Everything that moves, in the layout's own units, plus where its labels go."""
    key = layout["key"]
    vw = lambda x: f"{round(100.0 * x / layout['w'], 1)}vw"
    vh = lambda y: f"{round(100.0 * y / layout['h'], 1)}vh"
    sun = sun_xy(layout, state["fraction"]) if state["day"] else (layout["arc_cx"], layout["horizon"])
    # The flow is hidden at night, so its label needs no place then.
    box = solar_label_box(layout, sun) if state["day"] else (0, 0, 0, 0)
    data = {f"{key}SunX": sun[0], f"{key}SunY": sun[1], f"{key}SolarPath": quad(*solar_curve(layout, sun)),
            f"{key}SolarLabelX": vw((box[0] + box[2] - layout["label_slot"]) / 2.0),
            f"{key}SolarLabelY": vh(box[1]),
            f"{key}FillPath": battery_fill_path(layout, soc)}
    for gen, name in (("mep", "Mep"), ("kubota", "Kubota")):
        data[f"{key}{name}Path"] = quad(layout["gen_from"][gen], layout["gen_ctrl"][gen], layout["gen_to"][gen])
        label = layout["gen_label"][gen]
        data[f"{key}{name}LabelX"] = vw(label[0])
        data[f"{key}{name}LabelY"] = vh(label[1])
    return data

def build_apl_datasource(solar, spanish=False, thresholds=None, notice="", now=None):
    now = current_time() if now is None else now
    state = sky_state(now)
    soc = solar.get('batterySOC', 0)
    mppt80 = solar.get('mppt80PVPower', 0)
    south = solar.get('southArrayPVPower', 0)
    west = solar.get('westArrayPVPower', 0)
    total_solar = mppt80 + south + west
    load = (solar.get('acPower1') or 0) + (solar.get('acPower2') or 0)
    auto_enabled = solar.get('autoGenEnabled', True)
    # While a generator runs the inverters' AC output is the generator's, so the
    # house's draw can't be read. Same rule as the agent's load_w.
    states = {gen: generator_state(solar, gen) for gen in ("mep", "kubota")}
    house_known = "running" not in states.values()
    solar_flowing = state["day"] and total_solar > 50

    if spanish:
        auto_text = "Control automático activado" if auto_enabled else "Control automático desactivado"
        event = ("Atardecer " if state["day"] else "Amanecer ") + clock_text(state["next"], True)
        house_label = f"{kw_text(load)} casa"
    else:
        auto_text = "Auto control on" if auto_enabled else "Auto control off"
        event = ("Sunset " if state["day"] else "Sunrise ") + clock_text(state["next"], False)
        house_label = f"{kw_text(load)} house"

    data = {
        **PALETTE[state["day"]],
        "dayOpacity": 1 if state["day"] else 0,
        "nightOpacity": 0 if state["day"] else 1,
        "nowText": clock_text(now, spanish),
        "skyEvent": event,
        "autoText": auto_text,
        "autoDot": GREEN if auto_enabled else GEN_STATES["off"][0],
        "batterySOC": soc,
        "batteryVoltage": f"{solar.get('batteryVoltage', 0):.1f} V",
        "batteryColor": get_battery_color(soc),
        "solarFlow": solar_flowing,
        "solarOpacity": 1 if solar_flowing else 0,
        "solarKw": kw_text(total_solar),
        "solarDiv": flow_divisor(total_solar),
        "houseFlow": house_known,
        "houseOpacity": 1 if house_known else 0,
        "houseKw": kw_text(load),
        "houseLabel": house_label,
        "houseDiv": flow_divisor(load),
        "arrayTitle": "Solar por arreglo" if spanish else "Solar by array",
        "southLabel": "Sur" if spanish else "South",
        "westLabel": "Oeste" if spanish else "West",
        "mpptW": f"{mppt80} W",
        "southW": f"{south} W",
        "westW": f"{west} W",
        "mepThresholds": threshold_line(thresholds, 'mep803a', spanish),
        "kubotaThresholds": threshold_line(thresholds, 'kubota', spanish),
        "notice": notice,
    }
    for gen in ("mep", "kubota"):
        colour, english, spanish_word = GEN_STATES[states[gen]]
        watts = generator_charge_w(solar, gen)
        turning = generator_turning(solar, gen)
        data.update({f"{gen}Dot": colour, f"{gen}Word": spanish_word if spanish else english,
                     f"{gen}Flow": turning, f"{gen}Opacity": 1 if turning else 0,
                     f"{gen}Kw": kw_text(watts), f"{gen}Div": flow_divisor(watts)})
    data.update(scene_geometry(SHOW, state, soc))
    data.update(scene_geometry(SPOT, state, soc))
    return data

def build_response(speech, end_session=True, apl_document=None, apl_datasource=None):
    response = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": speech
            }
        }
    }
    # Left unset, a screen device keeps the session open without opening the mic.
    if end_session is not None:
        response["response"]["shouldEndSession"] = end_session
    
    if apl_document and apl_datasource:
        response["response"]["directives"] = [
            {
                "type": "Alexa.Presentation.APL.RenderDocument",
                "token": "solarDashboard",
                "document": apl_document,
                "datasources": {"solar": apl_datasource}
            }
        ]
    
    return jsonify(response)

def dashboard_response(speech, solar, spanish, end_session=None, notice="", thresholds_timeout=2):
    return build_response(speech, end_session=end_session,
                          apl_document=build_apl_document(),
                          apl_datasource=build_apl_datasource(
                              solar, spanish, get_thresholds(thresholds_timeout), notice))

def agent_screen_response(speech, question, spanish, started):
    """The answer on a screen. The Pi5's figures are extras: Alexa's clock is running."""
    lang = 'es' if spanish else 'en'
    in_time = lambda: time.monotonic() - started < AGENT_STATUS_DEADLINE
    solar = get_solar_data(AGENT_STATUS_TIMEOUT) if in_time() else None
    if speech == AGENT_LATER[lang]:
        notice = AGENT_SENT_TO_PHONE[lang]
        if solar:
            if in_time():
                return dashboard_response(speech, solar, spanish, notice=notice,
                                          thresholds_timeout=AGENT_STATUS_TIMEOUT)
            return build_response(speech, end_session=None, apl_document=build_apl_document(),
                                  apl_datasource=build_apl_datasource(solar, spanish, None, notice))
        return build_response(speech, end_session=None, apl_document=build_answer_document(),
                              apl_datasource=build_answer_datasource(question, notice, None, spanish))
    return build_response(speech, end_session=None, apl_document=build_answer_document(),
                          apl_datasource=build_answer_datasource(question, speech, solar, spanish))

@app.route('/alexa', methods=['POST'])
def alexa_handler():
    try:
        data = request.json
        request_type = data.get('request', {}).get('type', '')
        spanish = is_spanish(data)
        has_display = supports_apl(data)
        
        print(f"DEBUG: request_type={request_type}, spanish={spanish}, has_display={has_display}")
        
        if request_type == 'SessionEndedRequest':
            # A skill may not answer this one, and its body is the only place Alexa
            # says why a session failed: reason ERROR with error.type and message.
            ended = data.get('request', {})
            print(f"DEBUG SessionEnded: reason={ended.get('reason')} error={ended.get('error')}")
            return jsonify({"version": "1.0", "response": {}})

        if request_type == 'LaunchRequest':
            solar = get_solar_data()
            if spanish:
                speech = "Bienvenido a sistema solar. Puedes preguntar por estado de bateria, produccion solar, o controlar los generadores."
            else:
                speech = "Welcome to Solar System. You can ask for battery status, solar production, generator status, or control the generators."
            
            if has_display and solar:
                return dashboard_response(speech, solar, spanish, end_session=False)
            return build_response(speech, end_session=False)
        
        if request_type == 'IntentRequest':
            intent = data['request']['intent']['name']
            print(f"DEBUG: Intent={intent}")
            
            if intent == 'GetBatteryIntent':
                solar = get_solar_data()
                if solar is None:
                    speech = "Lo siento, no pude conectar al sistema solar." if spanish else "Sorry, I couldn't connect to the solar system."
                    return build_response(speech)
                soc = solar.get('batterySOC', 0)
                voltage = round(solar.get('batteryVoltage', 0), 2)
                if spanish:
                    speech = f"La bateria esta al {soc} por ciento con {voltage} voltios."
                else:
                    speech = f"Battery is at {soc} percent with {voltage} volts."
                
                if has_display:
                    return dashboard_response(speech, solar, spanish)
                return build_response(speech)
            
            elif intent == 'GetStatusIntent':
                solar = get_solar_data()
                if solar is None:
                    speech = "Lo siento, no pude conectar al sistema solar." if spanish else "Sorry, I couldn't connect to the solar system."
                    return build_response(speech)
                soc = solar.get('batterySOC', 0)
                voltage = round(solar.get('batteryVoltage', 0), 2)
                total_solar = solar.get('mppt80PVPower', 0) + solar.get('southArrayPVPower', 0) + solar.get('westArrayPVPower', 0)
                mep_mode = get_gen_mode(solar.get('mep803aMode', 0), spanish)
                kubota_mode = get_gen_mode(solar.get('kubotaMode', 0), spanish)
                auto_enabled = solar.get('autoGenEnabled', False)
                if spanish:
                    auto_status = "activado" if auto_enabled else "desactivado"
                    speech = f"Bateria al {soc} por ciento, {voltage} voltios. Solar produciendo {total_solar} watts. MEP 803 esta {mep_mode}. Kubota esta {kubota_mode}. Control automatico {auto_status}."
                else:
                    auto_status = "enabled" if auto_enabled else "disabled"
                    speech = f"Battery at {soc} percent, {voltage} volts. Solar producing {total_solar} watts. MEP 803 Alpha is {mep_mode}. Kubota is {kubota_mode}. Auto control {auto_status}."
                
                if has_display:
                    return dashboard_response(speech, solar, spanish)
                return build_response(speech)
            
            elif intent == 'GetSolarIntent':
                solar = get_solar_data()
                if solar is None:
                    speech = "Lo siento, no pude conectar al sistema solar." if spanish else "Sorry, I couldn't connect to the solar system."
                    return build_response(speech)
                mppt80 = solar.get('mppt80PVPower', 0)
                south = solar.get('southArrayPVPower', 0)
                west = solar.get('westArrayPVPower', 0)
                total = mppt80 + south + west
                if spanish:
                    speech = f"Produciendo {total} watts en total. MPPT 80 tiene {mppt80} watts, Arreglo Sur tiene {south} watts, Arreglo Oeste tiene {west} watts."
                else:
                    speech = f"Solar producing {total} watts total. MPPT 80 has {mppt80} watts, South Array has {south} watts, West Array has {west} watts."
                
                if has_display:
                    return dashboard_response(speech, solar, spanish)
                return build_response(speech)
            
            elif intent == 'GetGeneratorIntent':
                solar = get_solar_data()
                if solar is None:
                    speech = "Lo siento, no pude conectar al sistema solar." if spanish else "Sorry, I couldn't connect to the solar system."
                    return build_response(speech)
                mep_mode = get_gen_mode(solar.get('mep803aMode', 0), spanish)
                kubota_mode = get_gen_mode(solar.get('kubotaMode', 0), spanish)
                if spanish:
                    speech = f"MEP 803 esta {mep_mode}. Kubota esta {kubota_mode}."
                else:
                    speech = f"MEP 803 Alpha is {mep_mode}. Kubota is {kubota_mode}."
                
                if has_display:
                    return dashboard_response(speech, solar, spanish)
                return build_response(speech)
            
            elif intent == 'GetEventsIntent':
                events_data = get_events()
                if events_data is None:
                    speech = "Lo siento, no pude obtener los eventos." if spanish else "Sorry, I couldn't get the events."
                    return build_response(speech)
                
                events = events_data.get('events', [])
                recent = [e for e in events if e][:5]
                if not recent:
                    speech = "No hay eventos recientes." if spanish else "No recent events."
                else:
                    speech = ("Eventos recientes: " if spanish else "Recent events: ") + ". ".join(recent)
                if has_display:
                    return build_response(speech, end_session=None, apl_document=build_events_document(),
                                          apl_datasource=build_events_datasource(recent or [speech], spanish))
                return build_response(speech)
            
            elif intent == 'StartGeneratorIntent':
                confirmation = data['request'].get('intent', {}).get('confirmationStatus', '')
                generator = resolved_generator(data)
                
                if not generator and confirmation != 'DENIED':
                    return ask_which_generator(spanish)
                
                if confirmation == 'CONFIRMED':
                    gen_id = get_gen_id(generator)
                    if gen_id == 'all':
                        set_generator(50, 1)
                        set_generator(51, 1)
                        speech = "Prendiendo ambos generadores." if spanish else "Starting both generators."
                    else:
                        set_generator(gen_id, 1)
                        speech = f"Prendiendo el generador {spoken_generator(generator)}." if spanish else f"Starting the {spoken_generator(generator)} generator."
                elif confirmation == 'DENIED':
                    speech = "Está bien, no lo haré." if spanish else "OK, I won't."
                else:
                    speech = f"Confirma: prender el {spoken_generator(generator)}?" if spanish else f"Please confirm: start the {spoken_generator(generator)}?"
                    return build_response(speech, end_session=False)
                return build_response(speech)
            
            elif intent == 'StopGeneratorIntent':
                confirmation = data['request'].get('intent', {}).get('confirmationStatus', '')
                generator = resolved_generator(data)
                
                if not generator and confirmation != 'DENIED':
                    return ask_which_generator(spanish)
                
                if confirmation == 'CONFIRMED':
                    gen_id = get_gen_id(generator)
                    if gen_id == 'all':
                        # Use robust stop for both
                        stop_and_auto(50)
                        stop_and_auto(51)
                        speech = "Apagando ambos generadores. Esto tomara unos minutos." if spanish else "Stopping both generators. This will take a few minutes."
                    else:
                        # Use robust stop
                        stop_and_auto(gen_id)
                        speech = f"Apagando el generador {spoken_generator(generator)}. Esto tomara unos minutos." if spanish else f"Stopping the {spoken_generator(generator)} generator. This will take a few minutes."
                elif confirmation == 'DENIED':
                    speech = "Está bien, no lo haré." if spanish else "OK, I won't."
                else:
                    speech = f"Confirma: apagar el {spoken_generator(generator)}?" if spanish else f"Please confirm: stop the {spoken_generator(generator)}?"
                    return build_response(speech, end_session=False)
                return build_response(speech)
            
            elif intent == 'SetGeneratorAutoIntent':
                generator = resolved_generator(data)
                if not generator:
                    return ask_which_generator(spanish)
                
                gen_id = get_gen_id(generator)
                if gen_id == 'all':
                    set_generator(50, 2)
                    set_generator(51, 2)
                    speech = "Poniendo ambos generadores en automatico." if spanish else "Setting both generators to auto mode."
                else:
                    set_generator(gen_id, 2)
                    speech = f"Poniendo el generador {spoken_generator(generator)} en automatico." if spanish else f"Setting the {spoken_generator(generator)} generator to auto mode."
                return build_response(speech)
            
            elif intent == 'SetGeneratorOffIntent':
                print("DEBUG: SetGeneratorOffIntent triggered")
                confirmation = data['request'].get('intent', {}).get('confirmationStatus', '')
                generator = resolved_generator(data)
                
                if not generator and confirmation != 'DENIED':
                    return ask_which_generator(spanish)
                
                if confirmation == 'CONFIRMED':
                    gen_id = get_gen_id(generator)
                    if gen_id == 'all':
                        set_generator(50, 0)
                        set_generator(51, 0)
                        speech = "Poniendo ambos generadores en modo apagado." if spanish else "Setting both generators to off mode."
                    else:
                        set_generator(gen_id, 0)
                        speech = f"Poniendo el generador {spoken_generator(generator)} en modo apagado." if spanish else f"Setting the {spoken_generator(generator)} generator to off mode."
                elif confirmation == 'DENIED':
                    speech = "Está bien, no lo haré." if spanish else "OK, I won't."
                else:
                    speech = f"Confirma: poner el {spoken_generator(generator)} en modo apagado?" if spanish else f"Please confirm: set the {spoken_generator(generator)} to off?"
                    return build_response(speech, end_session=False)
                return build_response(speech)
            
            elif intent == 'EnableAutoGenIntent':
                print("DEBUG: EnableAutoGenIntent triggered")
                success, actual_state = set_autogen(True)
                print(f"DEBUG: EnableAutoGenIntent result: success={success}, actual_state={actual_state}")
                
                if success:
                    if spanish:
                        speech = "Control automatico de generadores activado."
                    else:
                        speech = "Automatic generator control enabled."
                else:
                    if spanish:
                        speech = f"Error al activar control automatico. Estado actual: {actual_state or 'desconocido'}."
                    else:
                        speech = f"Failed to enable auto control. Current state: {actual_state or 'unknown'}."
                return build_response(speech)
            
            elif intent == 'DisableAutoGenIntent':
                print("DEBUG: DisableAutoGenIntent triggered")
                success, actual_state = set_autogen(False)
                print(f"DEBUG: DisableAutoGenIntent result: success={success}, actual_state={actual_state}")
                
                if success:
                    if spanish:
                        speech = "Control automatico de generadores desactivado."
                    else:
                        speech = "Automatic generator control disabled."
                else:
                    if spanish:
                        speech = f"Error al desactivar control automatico. Estado actual: {actual_state or 'desconocido'}."
                    else:
                        speech = f"Failed to disable auto control. Current state: {actual_state or 'unknown'}."
                return build_response(speech)
            
            elif intent in AGENT_PREFIX:
                slots = data['request']['intent'].get('slots') or {}
                query = (slots.get('query') or {}).get('value') or ''
                query = query.strip()
                if not query:
                    speech = ("No entendi la pregunta." if spanish
                              else "I didn't catch the question.")
                    return build_response(speech, end_session=False)
                question = f"{AGENT_PREFIX[intent]} {query}".strip()
                print(f"DEBUG {intent}: {question}")
                started = time.monotonic()
                speech = ask_agent(question, 'es' if spanish else 'en')
                if has_display:
                    return agent_screen_response(speech, question, spanish, started)
                return build_response(speech)

            elif intent == 'AMAZON.HelpIntent':
                if spanish:
                    speech = "Puedes decir: estado de bateria, produccion solar, estado de generadores, que paso, activar control automatico, o desactivar control automatico. Para controlar generadores di: prende el Kubota, apaga el MEP, o pon los generadores en automatico. Tambien puedes preguntarle al agente, por ejemplo: pregunta al agente cual es el plan de esta noche."
                else:
                    speech = "You can say: battery status, solar production, generator status, what happened, enable auto control, or disable auto control. To control generators say: start the Kubota, stop the MEP, or put generators on auto. You can also ask the agent, for example: ask the agent what the plan is tonight."
                solar = get_solar_data() if has_display else None
                if solar:
                    return dashboard_response(speech, solar, spanish, end_session=False)
                return build_response(speech, end_session=False)
            
            elif intent in ['AMAZON.CancelIntent', 'AMAZON.StopIntent']:
                speech = "Adios!" if spanish else "Goodbye!"
                return build_response(speech)
            
            else:
                print(f"DEBUG: Unhandled intent: {intent}")
                speech = "No estoy seguro como manejar eso." if spanish else "I'm not sure how to handle that yet."
                return build_response(speech)
        
        speech = "No estoy seguro que pediste." if spanish else "I'm not sure what you asked for."
        return build_response(speech)
    
    except Exception as e:
        print(f"ERROR in alexa_handler: {e}")
        import traceback
        traceback.print_exc()
        return build_response("Sorry, there was an error.")

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
