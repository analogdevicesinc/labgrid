#!/usr/bin/env python3
# Toggle a board's reset line and power rail through an ADALM2000 shared
# between several places. Acquire the place first:
#   labgrid-client -p board-a acquire
import time

from labgrid import Environment

env = Environment("import-m2k.yaml")
target = env.get_target("main")

vcc = target.get_driver("M2KPowerSupplyDriver", name="vcc")
reset = target.get_driver("M2KDigitalIODriver", name="reset")
vbat = target.get_driver("AnalogInputProtocol", name="vbat_sense")

vcc.on()
print(f"V+ is {vcc.get_voltage():.2f} V")
print(f"VBAT is {vbat.get_voltage():.3f} V")
reset.set(False)  # assert nRESET
time.sleep(0.1)
reset.set(True)  # release nRESET
