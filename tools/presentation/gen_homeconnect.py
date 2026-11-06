"""Generate the SmartThings custom capabilities and device configs for Home Connect appliances.

silvervalley08660.applianceProgram: program (enum), supportedPrograms, setProgram, startProgram, start
silvervalley08660.cookSettings:     temperature (30..300 C), duration (minutes, 0 = no timer)
Labels in English and Dutch; the app picks the phone language. The keys must match HC_PROGRAMS in the bridge.
Run: python gen_homeconnect.py  (writes the JSON files next to this script)
"""
import json
from pathlib import Path

HERE = Path(__file__).parent

# key: (English, Dutch)
PROGRAMS = {
    "none": ("No program", "Geen programma"),
    # dishwasher
    "eco50": ("Eco 50°", "Eco 50°"),
    "auto1": ("Auto 35 to 45°", "Auto 35 tot 45°"),
    "auto2": ("Auto 45 to 65°", "Auto 45 tot 65°"),
    "auto3": ("Auto 65 to 75°", "Auto 65 tot 75°"),
    "intensive70": ("Intensive 70°", "Intensief 70°"),
    "intensive45": ("Intensive 45°", "Intensief 45°"),
    "quick45": ("Quick 45°", "Snel 45°"),
    "quick65": ("Quick 65°", "Snel 65°"),
    "super60": ("Super 60°", "Super 60°"),
    "glass40": ("Glass 40°", "Glas 40°"),
    "night": ("Silent", "Stil"),
    "machineCare": ("Machine care", "Machinereiniging"),
    "preRinse": ("Pre-rinse", "Voorspoelen"),
    "favorite": ("Favorite", "Favoriet"),
    # oven
    "hotAir": ("Hot air", "Hetelucht"),
    "hotAirEco": ("Hot air eco", "Hetelucht eco"),
    "topBottom": ("Top and bottom heat", "Boven- en onderwarmte"),
    "topBottomEco": ("Top and bottom heat eco", "Boven- en onderwarmte eco"),
    "hotAirGrill": ("Hot air grilling", "Circulatiegrill"),
    "pizza": ("Pizza setting", "Pizzastand"),
    "slowCook": ("Slow cook", "Garen op lage temperatuur"),
    "bottomHeat": ("Bottom heat", "Onderwarmte"),
    "keepWarm": ("Keep warm", "Warmhouden"),
    "preheatOvenware": ("Preheat ovenware", "Servies voorverwarmen"),
    "frozenFood": ("Frozen food", "Diepvriesproducten"),
    "grillLarge": ("Grill, large area", "Grill, groot oppervlak"),
    "grillSmall": ("Grill, small area", "Grill, klein oppervlak"),
    "sabbath": ("Sabbath", "Sabbat"),
    "microHotAir": ("Microwave + hot air", "Magnetron + hetelucht"),
    "microTopBottom": ("Microwave + top and bottom heat", "Magnetron + boven- en onderwarmte"),
    "microHotAirGrill": ("Microwave + hot air grilling", "Magnetron + circulatiegrill"),
    "microGrillLarge": ("Microwave + grill, large", "Magnetron + grill, groot"),
    "microGrillSmall": ("Microwave + grill, small", "Magnetron + grill, klein"),
    "micro90": ("Microwave 90 W", "Magnetron 90 W"),
    "micro180": ("Microwave 180 W", "Magnetron 180 W"),
    "micro360": ("Microwave 360 W", "Magnetron 360 W"),
    "micro600": ("Microwave 600 W", "Magnetron 600 W"),
    "micro900": ("Microwave 900 W", "Magnetron 900 W"),
    "microMax": ("Microwave max", "Magnetron max"),
    "drying": ("Drying", "Drogen"),
    # washer (Samsung)
    "cotton": ("Cotton", "Katoen"),
    "eCotton": ("E Cotton", "E Katoen"),
    "eco4060": ("Eco 40-60", "Eco 40-60"),
    "synthetics": ("Synthetics", "Synthetisch"),
    "delicates": ("Delicates", "Fijne was"),
    "wool": ("Wool", "Wol"),
    "bedding": ("Bedding", "Beddengoed"),
    "outdoor": ("Outdoor", "Outdoor"),
    "babyCare": ("Baby Care", "Babykleding"),
    "antiAllergen": ("Anti Allergen", "Anti-allergie"),
    "quick15": ("15' Quick Wash", "Snelwas 15'"),
    "quickWash": ("Quick Wash", "Snelwas"),
    "superSpeed": ("Super Speed", "Supersnel"),
    "superEco": ("Super Eco Wash", "Super Eco"),
    "mixed": ("Mixed Load", "Gemengde was"),
    "towels": ("Towels", "Handdoeken"),
    "denim": ("Denim", "Jeans"),
    "shirts": ("Shirts", "Overhemden"),
    "activeWear": ("Active Wear", "Sportkleding"),
    "colors": ("Colors", "Bonte was"),
    "whites": ("Whites", "Witte was"),
    "aiWash": ("AI Wash", "AI Wash"),
    "hygieneSteam": ("Hygiene Steam", "Hygiene stoom"),
    "rinseSpin": ("Rinse + Spin", "Spoelen + centrifugeren"),
    "spinOnly": ("Spin Only", "Alleen centrifugeren"),
    "drainSpin": ("Drain/Spin", "Pompen/centrifugeren"),
    "drumClean": ("Drum Clean", "Trommelreiniging"),
}
KEYS = list(PROGRAMS)


def write(name, obj):
    (HERE / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def alts(attr="program"):
    return [{"key": k, "value": f"{{{{i18n.attributes.{attr}.i18n.value.{k}.label}}}}", "type": "active"} for k in KEYS]


# ---------------------------------------------------------------- applianceProgram
enum = {"type": "string", "enum": KEYS}
write("applianceProgram.json", {
    "name": "Appliance Program",
    "attributes": {
        "program": {"schema": {"type": "object", "properties": {"value": enum}, "additionalProperties": False,
                               "required": ["value"]}, "setter": "setProgram", "enumCommands": []},
        "supportedPrograms": {"schema": {"type": "object", "properties": {"value": {"type": "array", "items": enum}},
                                         "additionalProperties": False, "required": ["value"]}, "enumCommands": []},
    },
    "commands": {
        "setProgram": {"name": "setProgram", "arguments": [{"name": "program", "optional": False, "schema": enum}]},
        "startProgram": {"name": "startProgram", "arguments": [{"name": "program", "optional": False, "schema": enum}]},
        "start": {"name": "start", "arguments": []},
    },
})
write("applianceProgram-pres.json", {
    "dashboard": {"states": [{"label": "{{program.value}}", "alternatives": alts()}], "actions": []},
    "detailView": [
        {"label": "{{i18n.label}}", "displayType": "list",
         "list": {"command": {"name": "setProgram", "alternatives": alts(), "argumentType": "string",
                              "supportedValues": "supportedPrograms.value"},
                  "state": {"value": "program.value", "valueType": "string", "alternatives": alts(),
                            "supportedValues": "supportedPrograms.value"}}},
        {"label": "{{i18n.commands.start.label}}", "displayType": "pushButton", "pushButton": {"command": "start"}},
    ],
    "automation": {
        "conditions": [{"label": "{{i18n.label}}", "displayType": "list",
                        "list": {"alternatives": alts(), "value": "program.value", "valueType": "string",
                                 "supportedValues": "supportedPrograms.value"}}],
        "actions": [
            {"label": "{{i18n.commands.startProgram.label}}", "displayType": "list",
             "list": {"alternatives": alts(), "command": "startProgram", "argumentType": "string",
                      "supportedValues": "supportedPrograms.value"}},
            {"label": "{{i18n.commands.setProgram.label}}", "displayType": "list",
             "list": {"alternatives": alts(), "command": "setProgram", "argumentType": "string",
                      "supportedValues": "supportedPrograms.value"}},
        ],
    },
})
for i, tag in enumerate(("en", "nl")):
    label = ("Program", "Programma")[i]
    write(f"applianceProgram-{tag}.json", {
        "tag": tag, "label": label,
        "attributes": {
            "program": {"label": label,
                        "displayTemplate": ("{{device.label}} program is {{value}}",
                                            "Programma van {{device.label}} is {{value}}")[i],
                        "i18n": {"value": {k: {"label": v[i]} for k, v in PROGRAMS.items()}}},
            "supportedPrograms": {"label": ("Supported programs", "Beschikbare programma's")[i]},
        },
        "commands": {
            "setProgram": {"label": ("Select program", "Programma kiezen")[i]},
            "startProgram": {"label": ("Start program", "Programma starten")[i]},
            "start": {"label": ("Start", "Starten")[i]},
        },
    })

# ---------------------------------------------------------------- cookSettings (oven)
write("cookSettings.json", {
    "name": "Cook Settings",
    "attributes": {
        "temperature": {"schema": {"type": "object", "properties": {
            "value": {"type": "number", "minimum": 30, "maximum": 300},
            "unit": {"type": "string", "enum": ["C", "F"], "default": "C"}},
            "additionalProperties": False, "required": ["value"]}, "setter": "setTemperature", "enumCommands": []},
        "duration": {"schema": {"type": "object", "properties": {
            "value": {"type": "integer", "minimum": 0, "maximum": 1440},
            "unit": {"type": "string", "enum": ["min"], "default": "min"}},
            "additionalProperties": False, "required": ["value"]}, "setter": "setDuration", "enumCommands": []},
    },
    "commands": {
        "setTemperature": {"name": "setTemperature", "arguments": [
            {"name": "temperature", "optional": False, "schema": {"type": "number", "minimum": 30, "maximum": 300}}]},
        "setDuration": {"name": "setDuration", "arguments": [
            {"name": "duration", "optional": False, "schema": {"type": "integer", "minimum": 0, "maximum": 1440}}]},
    },
})
write("cookSettings-pres.json", {
    "dashboard": {"states": [], "actions": []},
    "detailView": [
        {"label": "{{i18n.attributes.temperature.label}}", "displayType": "slider",
         "slider": {"range": [30, 300], "step": 5, "unit": "temperature.unit", "command": "setTemperature",
                    "argumentType": "number", "value": "temperature.value", "valueType": "number"}},
        {"label": "{{i18n.attributes.duration.label}}", "displayType": "slider",
         "slider": {"range": [0, 300], "step": 5, "unit": "duration.unit", "command": "setDuration",
                    "argumentType": "integer", "value": "duration.value", "valueType": "integer"}},
    ],
    "automation": {
        "conditions": [
            {"label": "{{i18n.attributes.temperature.label}}", "displayType": "numberField",
             "numberField": {"value": "temperature.value", "valueType": "number", "unit": "temperature.unit",
                             "range": [30, 300]}},
            {"label": "{{i18n.attributes.duration.label}}", "displayType": "numberField",
             "numberField": {"value": "duration.value", "valueType": "integer", "unit": "duration.unit",
                             "range": [0, 1440]}},
        ],
        "actions": [
            {"label": "{{i18n.attributes.temperature.label}}", "displayType": "numberField",
             "numberField": {"command": "setTemperature", "argumentType": "number", "unit": "temperature.unit",
                             "range": [30, 300]}},
            {"label": "{{i18n.attributes.duration.label}}", "displayType": "numberField",
             "numberField": {"command": "setDuration", "argumentType": "integer", "unit": "duration.unit",
                             "range": [0, 1440]}},
        ],
    },
})
for i, tag in enumerate(("en", "nl")):
    write(f"cookSettings-{tag}.json", {
        "tag": tag, "label": ("Cook settings", "Bereidingsinstellingen")[i],
        "attributes": {
            "temperature": {"label": ("Oven temperature", "Oventemperatuur")[i],
                            "displayTemplate": ("{{attribute}} of {{device.label}} is {{value}}",
                                                "{{attribute}} van {{device.label}} is {{value}}")[i]},
            "duration": {"label": ("Cooking time (0 = no timer)", "Bereidingstijd (0 = geen timer)")[i],
                         "displayTemplate": ("{{attribute}} of {{device.label}} is {{value}}",
                                             "{{attribute}} van {{device.label}} is {{value}}")[i]},
        },
        "commands": {
            "setTemperature": {"label": ("Set oven temperature", "Oventemperatuur instellen")[i]},
            "setDuration": {"label": ("Set cooking time", "Bereidingstijd instellen")[i]},
        },
    })


# ---------------------------------------------------------------- device configs
def item(cap, comp="main"):
    return {"component": comp, "capability": cap, "version": 1}


P, C = "silvervalley08660.applianceProgram", "silvervalley08660.cookSettings"
write("dishwasher-config.json", {
    "type": "profile", "mnmn": "SmartThingsCommunity",
    "dashboard": {"states": [item("dishwasherOperatingState")], "actions": [item("switch")]},
    "detailView": [item("switch"), item("dishwasherOperatingState"), item(P), item("contactSensor"),
                   item("remoteControlStatus"), item("refresh")],
    "automation": {
        "conditions": [item("switch"), item("dishwasherOperatingState"), item(P), item("contactSensor"),
                       item("remoteControlStatus")],
        "actions": [item("switch"), item(P), item("dishwasherOperatingState")],
    },
})
write("washer-config.json", {
    "type": "profile", "mnmn": "SmartThingsCommunity",
    "dashboard": {"states": [item("washerOperatingState")], "actions": [item("switch")]},
    "detailView": [item("switch"), item("washerOperatingState"), item(P), item("remoteControlStatus"), item("refresh")],
    "automation": {
        "conditions": [item("switch"), item("washerOperatingState"), item(P), item("remoteControlStatus")],
        "actions": [item("switch"), item(P), item("washerOperatingState")],
    },
})
write("oven-config.json", {
    "type": "profile", "mnmn": "SmartThingsCommunity",
    "dashboard": {"states": [item("temperatureMeasurement")], "actions": [item("switch")]},
    "detailView": [item("switch"), item("ovenOperatingState"), item(P), item(C), item("temperatureMeasurement"),
                   item("contactSensor"), item("remoteControlStatus"), item("refresh")],
    "automation": {
        "conditions": [item("switch"), item("ovenOperatingState"), item(P), item(C), item("temperatureMeasurement"),
                       item("contactSensor"), item("remoteControlStatus")],
        "actions": [item("switch"), item(C), item(P), item("ovenOperatingState")],
    },
})
print("written", len(KEYS), "programs")
