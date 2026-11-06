"""Home Connect profiles (zips from the Home Connect Profile Downloader) and helpers."""
import json
import zipfile

import stoeckie_bridge as sb


def make_zip(path, ha_id, typ, brand="SIEMENS", conn="TLS", iv=None):
    info = {"haId": ha_id, "type": typ, "brand": brand, "vib": "X1", "connectionType": conn, "key": "secret",
            "featureMappingFileName": f"{ha_id}_FeatureMapping.xml",
            "deviceDescriptionFileName": f"{ha_id}_DeviceDescription.xml"}
    if iv:
        info["iv"] = iv
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{ha_id}.json", json.dumps(info))
        z.writestr(info["featureMappingFileName"], "<featureMappingFile/>")
        z.writestr(info["deviceDescriptionFileName"], "<device/>")


def test_profiles_and_hosts(tmp_path):
    make_zip(tmp_path / "dish.zip", "015100394724004530", "Dishwasher")
    make_zip(tmp_path / "oven.zip", "SIEMENS-CM676G0S6-68A40E6EC669", "Oven", conn="AES", iv="iv")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    profs = sb.homeconnect_profiles(tmp_path)
    assert sorted(p["type"] for p in profs) == ["Dishwasher", "Oven"]
    assert all(p["xml"][0] == "<device/>" for p in profs)
    hosts = {p["type"]: sb.hc_default_host(p) for p in profs}
    assert hosts == {"Dishwasher": "siemens-dishwasher-015100394724004530.local",
                     "Oven": "SIEMENS-CM676G0S6-68A40E6EC669.local"}
    assert sb.homeconnect_profiles(tmp_path / "missing") == []


class E:
    def __init__(self, v, enum=None):
        self.value, self.enum = v, enum


class App:
    settings = {"BSH.Common.Setting.PowerState": E("On")}
    status = {"BSH.Common.Status.OperationState": E("Run"), "BSH.Common.Status.DoorState": E("Closed"),
              "BSH.Common.Status.RemoteControlStartAllowed": E(True),
              "Cooking.Oven.Status.CurrentCavityTemperature": E(165)}
    options = {"BSH.Common.Option.RemainingProgramTime": E(600), "BSH.Common.Option.ProgramProgress": E(30),
               "Cooking.Oven.Option.SetpointTemperature": E(180)}

    @property
    def active_program(self):
        return type("P", (), {"name": "Cooking.Oven.Program.HeatingMode.HotAir"})()


def test_state():
    st = sb.hc_state(App(), "oven")
    assert st["power"] == "on" and st["operation"] == "run" and st["door"] == "closed"
    assert st["program"] == "HotAir" and st["remainingTime"] == 600 and st["progress"] == 30
    assert st["temperature"] == 165.0 and st["setpoint"] == 180.0 and st["remoteStartAllowed"] is True
    assert st["completionTime"]
