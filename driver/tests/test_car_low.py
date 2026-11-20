from lupa import lua53
import pathlib
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "init.lua"


def load():
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    t = SRC.read_text(encoding="utf-8")
    a = t.index("local CAR_LOW, CAR_LOW_HOLD")
    b = t.index("-- end car low")
    lua.execute(t[a:b] + "\nSHOWN = car_shown_soc")
    dev = lua.eval("""{ f = {},
      get_field = function(self, k) return self.f[k] end,
      set_field = function(self, k, v) self.f[k] = v end }""")
    return lua.globals().SHOWN, dev


def test_low_only_after_12_hours():
    shown, dev = load()
    assert shown(dev, 57, 0) == 57
    assert shown(dev, 0, 600) == 57            # wrong 0 % reading: keep 57
    assert shown(dev, 55, 1200) == 55          # back up: timer reset
    assert shown(dev, 8, 2000) == 55
    assert shown(dev, 7, 2000 + 11 * 3600) == 55
    assert shown(dev, 7, 2000 + 12 * 3600) == 7  # low for 12 h: real value, SmartThings may warn
    assert shown(dev, 6, 2000 + 13 * 3600) == 6


def test_no_known_value_shows_threshold():
    shown, dev = load()
    assert shown(dev, 0, 100) == 10
