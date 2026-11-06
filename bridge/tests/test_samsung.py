"""Samsung local: client certificate."""
from cryptography import x509

import stoeckie_bridge as sb


def test_cert(tmp_path):
    c, k = sb.samsung_cert(tmp_path)
    cert = x509.load_pem_x509_certificate(c.read_bytes())
    subj = cert.subject.rfc4514_string()
    assert f"uuid:{sb.SS_CLIENT_UUID}" in subj and f"urn:uuid:{sb.SS_CLIENT_UUID}" in subj
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sb.SS_CLIENT_UUID in san.get_values_for_type(x509.DNSName)
    assert b"PRIVATE KEY" in k.read_bytes()
    assert sb.samsung_cert(tmp_path) == (c, k)       # made once, reused
    assert c.read_bytes() == sb.samsung_cert(tmp_path)[0].read_bytes()


def test_washer_state():
    res = {
        "/operational/state/vs/0": {"x.com.samsung.da.state": "Run", "x.com.samsung.da.progress": "Rinse",
                                    "x.com.samsung.da.remainingTime": "00:42:00",
                                    "x.com.samsung.da.progressPercentage": "60"},
        "/power/vs/0": {"x.com.samsung.da.power": "On"},
        "/remotectrl/vs/0": {"x.com.samsung.da.remoteControlEnabled": "true"},
        "/course/vs/0": {"x.com.samsung.da.options": ["Course_1C", "DrumCleanProposal_40"]},
        "/st/washercourse/vs/0": {"x.com.samsung.da.st.courseTable": "Table_02"},
    }
    st = sb.ss_washer_state(res)
    assert st["power"] == "on" and st["operation"] == "run" and st["job"] == "rinse"
    assert st["remainingTime"] == 2520 and st["progress"] == 60 and st["completionTime"]
    assert st["remoteStartAllowed"] is True and st["course"] == "Eco 40-60"
    res["/operational/state/vs/0"].update({"x.com.samsung.da.progress": "Finish"})
    st = sb.ss_washer_state(res)
    assert st["operation"] == "finished" and st["job"] == "finish" and st["remainingTime"] is None
    off = sb.ss_washer_state({})
    assert off["power"] == "off" and off["operation"] == "inactive" and off["course"] is None
    assert sb.ss_seconds("12:30") == 750 and sb.ss_seconds("x") is None
    assert sb.ss_course_label("ZZ", "Table_99") == "Course ZZ"


def test_washer_commands(monkeypatch):
    import asyncio
    import pytest
    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.9"}]}})
    writes = []
    monkeypatch.setattr(sb.Samsung, "_write", staticmethod(lambda sess, path, body: writes.append((path, body))))
    ss.locks["192.168.1.9"] = asyncio.Lock()
    h = ss._command("192.168.1.9")
    with pytest.raises(ValueError):
        asyncio.run(h("pause", None))             # not connected
    ss.sessions["192.168.1.9"] = object()
    asyncio.run(h("start", None))
    asyncio.run(h("stop", None))
    asyncio.run(h("off", None))
    assert writes[0] == (("operational", "state", "vs", "0"), {"x.com.samsung.da.state": "Run"})
    assert writes[1][1] == {"x.com.samsung.da.state": "Ready"}
    assert writes[2] == (("power", "vs", "0"), {"x.com.samsung.da.power": "Off"})
