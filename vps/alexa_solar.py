#!/usr/bin/env python3
"""
Alexa Solar Flask Backend V3
- Uses new /stopgen endpoint for robust stop sequence
- No more timing guesswork - ESP8266 handles entire stop→auto sequence
- stop_and_auto() is now a simple single call with longer timeout
"""
import re
from flask import Flask, request, jsonify
import requests
import threading
import time

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

def get_gen_mode_color(mode):
    if mode == 0:
        return "#FF6B6B"
    elif mode == 1:
        return "#00E676"
    elif mode == 2:
        return "#FFD93D"
    return "#9E9E9E"

def get_battery_color(soc):
    if soc >= 70:
        return "#00E676"
    elif soc >= 40:
        return "#FFD93D"
    else:
        return "#FF6B6B"

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

def clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

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

def apl_screen(items):
    return {
        "type": "APL",
        "version": "1.6",
        "theme": "dark",
        "settings": {"idleTimeout": 60000},
        "mainTemplate": {"parameters": ["payload"], "items": items},
    }

def round_text(text, color, size, **extra):
    return {"type": "Text", "text": text, "color": color, "fontSize": size,
            "width": "100%", "textAlign": "center", **extra}

def round_dashboard():
    return {
        "type": "Container",
        "when": ROUND,
        "width": "100vw",
        "height": "100vh",
        "justifyContent": "center",
        "paddingLeft": "15vw",
        "paddingRight": "15vw",
        "paddingTop": "15vh",
        "paddingBottom": "15vh",
        "items": [
            round_text("${payload.batterySOC}%", "${payload.batteryColor}", "16vh", fontWeight="bold"),
            round_text("${payload.batteryVoltage} V", "#FFFFFF", "6vh"),
            round_text("${payload.totalSolar} W", "#FFD93D", "6vh"),
            round_text("MEP-803A: ${payload.mepStatus}", "${payload.mepColor}", "4vh"),
            round_text("${payload.mepThresholds}", "#B0BEC5", "3vh", when="${payload.mepThresholds != ''}"),
            round_text("KUBOTA: ${payload.kubotaStatus}", "${payload.kubotaColor}", "4vh"),
            round_text("${payload.kubotaThresholds}", "#B0BEC5", "3vh", when="${payload.kubotaThresholds != ''}"),
            round_text("${payload.notice}", "#BBDEFB", "3vh", when="${payload.notice != ''}"),
        ]
    }

def build_answer_document():
    return apl_screen([{
        "type": "Container",
        "width": "100vw",
        "height": "100vh",
        **PADDING,
        "items": [
            {"type": "Text", "text": "${payload.question}", "color": "#BBDEFB", "fontSize": "4vh",
             "maxLines": 2, "textAlign": TEXT_ALIGN},
            {"type": "ScrollView", "grow": 1, "shrink": 1, "marginTop": "2vh", "item": {
                "type": "Text", "text": "${payload.answer}", "color": "#FFFFFF",
                "fontSize": "${viewport.shape == 'round' ? '4vh' : '5vh'}", "textAlign": TEXT_ALIGN}},
            {"type": "Text", "when": "${payload.statusLine != ''}", "text": "${payload.statusLine}",
             "color": "${payload.statusColor}", "fontSize": "3.5vh", "marginTop": "2vh", "textAlign": "center"},
            {"type": "Text", "when": "${payload.genLine != ''}", "text": "${payload.genLine}",
             "color": "#B0BEC5", "fontSize": "3vh", "textAlign": "center"},
        ]
    }])

def build_answer_datasource(question, answer, solar, spanish):
    payload = {"question": clip(question, SCREEN_QUESTION_CHARS),
               "answer": clip(answer, SCREEN_ANSWER_CHARS),
               "statusLine": "", "statusColor": "#FFFFFF", "genLine": ""}
    if solar:
        soc = solar.get('batterySOC', 0)
        voltage = round(solar.get('batteryVoltage', 0), 2)
        total = (solar.get('mppt80PVPower', 0) + solar.get('southArrayPVPower', 0)
                 + solar.get('westArrayPVPower', 0))
        mep = get_gen_mode(solar.get('mep803aMode', 0), spanish).upper()
        kubota = get_gen_mode(solar.get('kubotaMode', 0), spanish).upper()
        payload.update(statusLine=f"{soc}% · {voltage} V · {total} W",
                       statusColor=get_battery_color(soc),
                       genLine=f"MEP-803A: {mep} · KUBOTA: {kubota}")
    return payload

def build_events_document():
    return apl_screen([{
        "type": "Container",
        "width": "100vw",
        "height": "100vh",
        **PADDING,
        "items": [
            {"type": "Text", "text": "${payload.title}", "color": "#FFD93D", "fontSize": "5vh",
             "fontWeight": "bold", "textAlign": "center"},
            {"type": "ScrollView", "grow": 1, "shrink": 1, "marginTop": "2vh", "item": {
                "type": "Container", "width": "100%", "items": [{
                    "type": "Text", "data": "${payload.events}", "text": "${data}", "color": "#FFFFFF",
                    "fontSize": "${viewport.shape == 'round' ? '3.5vh' : '4vh'}",
                    "paddingBottom": "2vh", "textAlign": TEXT_ALIGN}]}},
        ]
    }])

def build_apl_document():
    return {
        "type": "APL",
        "version": "1.6",
        "theme": "dark",
        "settings": {"idleTimeout": 60000},
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                round_dashboard(),
                {
                    "type": "Container",
                    "when": NOT_ROUND,
                    "width": "100vw",
                    "height": "100vh",
                    "style": "background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%)",
                    "items": [
                        {
                            "type": "Container",
                            "width": "100%",
                            "height": "100%",
                            "paddingLeft": "5vw",
                            "paddingRight": "5vw",
                            "paddingTop": "3vh",
                            "items": [
                                {
                                    "type": "Text",
                                    "text": "☀️ ${payload.title}",
                                    "style": "textStyleDisplay4",
                                    "textAlign": "center",
                                    "color": "#FFD93D",
                                    "fontWeight": "bold",
                                    "fontSize": "6vh"
                                },
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "width": "100%",
                                    "height": "35vh",
                                    "marginTop": "3vh",
                                    "justifyContent": "spaceBetween",
                                    "items": [
                                        {
                                            "type": "Container",
                                            "width": "48%",
                                            "height": "100%",
                                            "alignItems": "center",
                                            "justifyContent": "center",
                                            "style": "background: rgba(255,255,255,0.1); borderRadius: 20px",
                                            "items": [
                                                {"type": "Text", "text": "🔋 ${payload.batteryLabel}", "color": "#BBDEFB", "fontSize": "3vh"},
                                                {"type": "Text", "text": "${payload.batterySOC}%", "color": "${payload.batteryColor}", "fontSize": "12vh", "fontWeight": "bold"},
                                                {"type": "Text", "text": "${payload.batteryVoltage}V", "color": "#FFFFFF", "fontSize": "5vh"}
                                            ]
                                        },
                                        {
                                            "type": "Container",
                                            "width": "48%",
                                            "height": "100%",
                                            "alignItems": "center",
                                            "justifyContent": "center",
                                            "style": "background: rgba(255,255,255,0.1); borderRadius: 20px",
                                            "items": [
                                                {"type": "Text", "text": "☀️ ${payload.solarLabel}", "color": "#BBDEFB", "fontSize": "3vh"},
                                                {"type": "Text", "text": "${payload.totalSolar}W", "color": "#FFD93D", "fontSize": "12vh", "fontWeight": "bold"},
                                                {"type": "Text", "text": "${payload.solarDetails}", "color": "#B0BEC5", "fontSize": "2.5vh", "textAlign": "center"}
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "width": "100%",
                                    "height": "25vh",
                                    "marginTop": "3vh",
                                    "justifyContent": "spaceBetween",
                                    "items": [
                                        {
                                            "type": "Container",
                                            "width": "48%",
                                            "height": "100%",
                                            "alignItems": "center",
                                            "justifyContent": "center",
                                            "style": "background: rgba(255,255,255,0.1); borderRadius: 20px",
                                            "items": [
                                                {"type": "Text", "text": "⚡ MEP-803A", "color": "#BBDEFB", "fontSize": "3vh"},
                                                {"type": "Text", "text": "${payload.mepStatus}", "color": "${payload.mepColor}", "fontSize": "8vh", "fontWeight": "bold"},
                                                {"type": "Text", "when": "${payload.mepThresholds != ''}", "text": "${payload.mepThresholds}", "color": "#B0BEC5", "fontSize": "3vh"}
                                            ]
                                        },
                                        {
                                            "type": "Container",
                                            "width": "48%",
                                            "height": "100%",
                                            "alignItems": "center",
                                            "justifyContent": "center",
                                            "style": "background: rgba(255,255,255,0.1); borderRadius: 20px",
                                            "items": [
                                                {"type": "Text", "text": "⚡ KUBOTA", "color": "#BBDEFB", "fontSize": "3vh"},
                                                {"type": "Text", "text": "${payload.kubotaStatus}", "color": "${payload.kubotaColor}", "fontSize": "8vh", "fontWeight": "bold"},
                                                {"type": "Text", "when": "${payload.kubotaThresholds != ''}", "text": "${payload.kubotaThresholds}", "color": "#B0BEC5", "fontSize": "3vh"}
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "type": "Container",
                                    "width": "100%",
                                    "height": "10vh",
                                    "marginTop": "2vh",
                                    "alignItems": "center",
                                    "justifyContent": "center",
                                    "items": [
                                        {"type": "Text", "text": "🤖 ${payload.autoControlLabel}: ${payload.autoControl}", "color": "${payload.autoControlColor}", "fontSize": "3vh"},
                                        {"type": "Text", "when": "${payload.notice != ''}", "text": "${payload.notice}", "color": "#BBDEFB", "fontSize": "3vh"}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

def build_apl_datasource(solar, spanish=False, thresholds=None, notice=""):
    soc = solar.get('batterySOC', 0)
    voltage = round(solar.get('batteryVoltage', 0), 2)
    mppt80 = solar.get('mppt80PVPower', 0)
    south = solar.get('southArrayPVPower', 0)
    west = solar.get('westArrayPVPower', 0)
    total_solar = mppt80 + south + west
    mep_mode = solar.get('mep803aMode', 0)
    kubota_mode = solar.get('kubotaMode', 0)
    auto_enabled = solar.get('autoGenEnabled', True)
    
    if spanish:
        return {
            "title": "MI SISTEMA SOLAR",
            "batteryLabel": "BATERÍA",
            "batterySOC": soc,
            "batteryVoltage": voltage,
            "batteryColor": get_battery_color(soc),
            "solarLabel": "PRODUCCIÓN",
            "totalSolar": total_solar,
            "solarDetails": f"MPPT80: {mppt80}W | Sur: {south}W | Oeste: {west}W",
            "mepStatus": get_gen_mode(mep_mode, True).upper(),
            "mepColor": get_gen_mode_color(mep_mode),
            "kubotaStatus": get_gen_mode(kubota_mode, True).upper(),
            "kubotaColor": get_gen_mode_color(kubota_mode),
            "autoControlLabel": "CONTROL AUTO",
            "autoControl": "ACTIVADO" if auto_enabled else "DESACTIVADO",
            "autoControlColor": "#00E676" if auto_enabled else "#FF6B6B",
            "mepThresholds": threshold_line(thresholds, 'mep803a', spanish),
            "kubotaThresholds": threshold_line(thresholds, 'kubota', spanish),
            "notice": notice
        }
    else:
        return {
            "title": "MY SOLAR SYSTEM",
            "batteryLabel": "BATTERY",
            "batterySOC": soc,
            "batteryVoltage": voltage,
            "batteryColor": get_battery_color(soc),
            "solarLabel": "PRODUCTION",
            "totalSolar": total_solar,
            "solarDetails": f"MPPT80: {mppt80}W | South: {south}W | West: {west}W",
            "mepStatus": get_gen_mode(mep_mode, False),
            "mepColor": get_gen_mode_color(mep_mode),
            "kubotaStatus": get_gen_mode(kubota_mode, False),
            "kubotaColor": get_gen_mode_color(kubota_mode),
            "autoControlLabel": "AUTO CONTROL",
            "autoControl": "ENABLED" if auto_enabled else "DISABLED",
            "autoControlColor": "#00E676" if auto_enabled else "#FF6B6B",
            "mepThresholds": threshold_line(thresholds, 'mep803a', spanish),
            "kubotaThresholds": threshold_line(thresholds, 'kubota', spanish),
            "notice": notice
        }

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
                "datasources": {"payload": apl_datasource}
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
                                          apl_datasource={
                                              "title": "EVENTOS RECIENTES" if spanish else "RECENT EVENTS",
                                              "events": recent or [speech]})
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
