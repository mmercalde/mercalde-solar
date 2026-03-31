#!/usr/bin/env python3
"""
Alexa Solar Flask Backend V3
- Uses new /stopgen endpoint for robust stop sequence
- No more timing guesswork - ESP8266 handles entire stop→auto sequence
- stop_and_auto() is now a simple single call with longer timeout
"""
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

def get_solar_data():
    try:
        resp = requests.get(ESP8266_DATA_URL, timeout=5)
        return resp.json()
    except Exception as e:
        print(f"ERROR get_solar_data: {e}")
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
    name = generator_name.lower()
    if 'kubota' in name:
        return 50
    elif 'mep' in name or 'military' in name or 'militar' in name:
        return 51
    elif 'all' in name or 'both' in name or 'todos' in name or 'ambos' in name:
        return 'all'
    return None

def is_spanish(data):
    locale = data.get('request', {}).get('locale', 'en-US')
    return locale.startswith('es')

def supports_apl(data):
    try:
        supported = data.get('context', {}).get('System', {}).get('device', {}).get('supportedInterfaces', {})
        return 'Alexa.Presentation.APL' in supported
    except:
        return False

def build_apl_document():
    return {
        "type": "APL",
        "version": "1.6",
        "theme": "dark",
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                {
                    "type": "Container",
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
                                                {"type": "Text", "text": "${payload.mepStatus}", "color": "${payload.mepColor}", "fontSize": "8vh", "fontWeight": "bold"}
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
                                                {"type": "Text", "text": "${payload.kubotaStatus}", "color": "${payload.kubotaColor}", "fontSize": "8vh", "fontWeight": "bold"}
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
                                        {"type": "Text", "text": "🤖 ${payload.autoControlLabel}: ${payload.autoControl}", "color": "${payload.autoControlColor}", "fontSize": "3vh"}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }

def build_apl_datasource(solar, spanish=False):
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
            "autoControlColor": "#00E676" if auto_enabled else "#FF6B6B"
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
            "autoControlColor": "#00E676" if auto_enabled else "#FF6B6B"
        }

def build_response(speech, end_session=True, apl_document=None, apl_datasource=None):
    response = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": speech
            },
            "shouldEndSession": end_session
        }
    }
    
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

@app.route('/alexa', methods=['POST'])
def alexa_handler():
    try:
        data = request.json
        request_type = data.get('request', {}).get('type', '')
        spanish = is_spanish(data)
        has_display = supports_apl(data)
        
        print(f"DEBUG: request_type={request_type}, spanish={spanish}, has_display={has_display}")
        
        if request_type == 'LaunchRequest':
            solar = get_solar_data()
            if spanish:
                speech = "Bienvenido a sistema solar. Puedes preguntar por estado de bateria, produccion solar, o controlar los generadores."
            else:
                speech = "Welcome to Solar System. You can ask for battery status, solar production, generator status, or control the generators."
            
            if has_display and solar:
                return build_response(speech, end_session=False, 
                                     apl_document=build_apl_document(),
                                     apl_datasource=build_apl_datasource(solar, spanish))
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
                    return build_response(speech, apl_document=build_apl_document(),
                                         apl_datasource=build_apl_datasource(solar, spanish))
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
                    return build_response(speech, apl_document=build_apl_document(),
                                         apl_datasource=build_apl_datasource(solar, spanish))
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
                    return build_response(speech, apl_document=build_apl_document(),
                                         apl_datasource=build_apl_datasource(solar, spanish))
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
                    return build_response(speech, apl_document=build_apl_document(),
                                         apl_datasource=build_apl_datasource(solar, spanish))
                return build_response(speech)
            
            elif intent == 'GetEventsIntent':
                events_data = get_events()
                if events_data is None:
                    speech = "Lo siento, no pude obtener los eventos." if spanish else "Sorry, I couldn't get the events."
                    return build_response(speech)
                
                events = events_data.get('events', [])
                if not events or all(e == '' for e in events):
                    speech = "No hay eventos recientes." if spanish else "No recent events."
                else:
                    recent = [e for e in events if e][:5]
                    speech = ("Eventos recientes: " if spanish else "Recent events: ") + ". ".join(recent)
                return build_response(speech)
            
            elif intent == 'StartGeneratorIntent':
                confirmation = data['request'].get('intent', {}).get('confirmationStatus', '')
                slots = data['request'].get('intent', {}).get('slots', {})
                generator = slots.get('generator', {}).get('value', 'unknown')
                
                if confirmation == 'CONFIRMED':
                    gen_id = get_gen_id(generator)
                    if gen_id == 'all':
                        set_generator(50, 1)
                        set_generator(51, 1)
                        speech = "Prendiendo ambos generadores." if spanish else "Starting both generators."
                    elif gen_id:
                        set_generator(gen_id, 1)
                        speech = f"Prendiendo el generador {generator}." if spanish else f"Starting the {generator} generator."
                    else:
                        speech = f"No reconozco el generador {generator}." if spanish else f"I don't recognize the generator {generator}."
                elif confirmation == 'DENIED':
                    speech = "Okay, no prendo el generador." if spanish else "Okay, not starting the generator."
                else:
                    speech = f"Confirma: prender el {generator}?" if spanish else f"Please confirm: start the {generator}?"
                    return build_response(speech, end_session=False)
                return build_response(speech)
            
            elif intent == 'StopGeneratorIntent':
                confirmation = data['request'].get('intent', {}).get('confirmationStatus', '')
                slots = data['request'].get('intent', {}).get('slots', {})
                generator = slots.get('generator', {}).get('value', 'unknown')
                
                if confirmation == 'CONFIRMED':
                    gen_id = get_gen_id(generator)
                    if gen_id == 'all':
                        # Use robust stop for both
                        stop_and_auto(50)
                        stop_and_auto(51)
                        speech = "Apagando ambos generadores. Esto tomara unos minutos." if spanish else "Stopping both generators. This will take a few minutes."
                    elif gen_id:
                        # Use robust stop
                        stop_and_auto(gen_id)
                        speech = f"Apagando el generador {generator}. Esto tomara unos minutos." if spanish else f"Stopping the {generator} generator. This will take a few minutes."
                    else:
                        speech = f"No reconozco el generador {generator}." if spanish else f"I don't recognize the generator {generator}."
                elif confirmation == 'DENIED':
                    speech = "Okay, no apago el generador." if spanish else "Okay, not stopping the generator."
                else:
                    speech = f"Confirma: apagar el {generator}?" if spanish else f"Please confirm: stop the {generator}?"
                    return build_response(speech, end_session=False)
                return build_response(speech)
            
            elif intent == 'SetGeneratorAutoIntent':
                slots = data['request'].get('intent', {}).get('slots', {})
                generator = slots.get('generator', {}).get('value', 'unknown')
                
                gen_id = get_gen_id(generator)
                if gen_id == 'all':
                    set_generator(50, 2)
                    set_generator(51, 2)
                    speech = "Poniendo ambos generadores en automatico." if spanish else "Setting both generators to auto mode."
                elif gen_id:
                    set_generator(gen_id, 2)
                    speech = f"Poniendo el generador {generator} en automatico." if spanish else f"Setting the {generator} generator to auto mode."
                else:
                    speech = f"No reconozco el generador {generator}." if spanish else f"I don't recognize the generator {generator}."
                return build_response(speech)
            
            elif intent == 'SetGeneratorOffIntent':
                print("DEBUG: SetGeneratorOffIntent triggered")
                slots = data['request'].get('intent', {}).get('slots', {})
                generator = slots.get('generator', {}).get('value', 'unknown')
                
                gen_id = get_gen_id(generator)
                if gen_id == 'all':
                    set_generator(50, 0)
                    set_generator(51, 0)
                    speech = "Poniendo ambos generadores en modo apagado." if spanish else "Setting both generators to off mode."
                elif gen_id:
                    set_generator(gen_id, 0)
                    speech = f"Poniendo el generador {generator} en modo apagado." if spanish else f"Setting the {generator} generator to off mode."
                else:
                    speech = f"No reconozco el generador {generator}." if spanish else f"I don't recognize the generator {generator}."
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
            
            elif intent == 'AMAZON.HelpIntent':
                if spanish:
                    speech = "Puedes decir: estado de bateria, produccion solar, estado de generadores, que paso, activar control automatico, o desactivar control automatico. Para controlar generadores di: prende el Kubota, apaga el MEP, o pon los generadores en automatico."
                else:
                    speech = "You can say: battery status, solar production, generator status, what happened, enable auto control, or disable auto control. To control generators say: start the Kubota, stop the MEP, or put generators on auto."
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
