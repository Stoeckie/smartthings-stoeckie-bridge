from lupa import lua53
import pathlib
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "init.lua"


def test_poll_off_right_after_mode_command_counts_as_on():
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    t = SRC.read_text(encoding="utf-8")
    a = t.index("local AC_HOLD = {}")
    b = t.index("-- Swing toggles (components")
    lua.execute("SWING_TO_ST, SWING_FROM_ST, AC_MODE_TO_ST, AC_MODE_FROM_ST = {}, {}, {heat='heat'}, {heat='heat'}\n"
                + t[a:b] + "\nAPPLY, AC_ON_T, RECENT = apply_holds, AC_ON, recently_on")
    dev = lua.eval("{ id = 'd1' }")
    s = lua.eval("{ power = 'off', setpoint = 25 }")
    assert lua.globals().APPLY(dev, s, s).power == "off"
    lua.execute("AC_ON_T['d1'] = os.time()")
    assert lua.globals().RECENT(dev)
    assert lua.globals().APPLY(dev, s, s).power == "on"
    lua.execute("AC_ON_T['d1'] = os.time() - 120")
    assert lua.globals().APPLY(dev, s, s).power == "off"
